"""L4 工作记忆辅助 —— 写入时压缩，仅保留 Top-K 关键字段。

工作记忆本体在 LangGraph State 的 `working_memory`（Annotated[..., add] 自动累加），
本模块不持有状态，只提供「把单条/整批工具记录压成给 LLM 看的精简版」的纯函数：
- 大块 result 文本截断到上限，避免污染上下文
- 只保留决策真正需要的关键字段（工具名 / 入参 / 成败 / 耗时）

Working memory vs Trace（设计要点）：
- Working = 给「当前这次推理的 LLM」看的依据，频繁写、几乎不持久化；
- Trace（Langfuse，W5）= 给「事后复盘的工程师」看的投影，落盘留存。
两者同源不同出口 —— 同一份工具调用数据，一个进 prompt、一个进可观测系统。
"""

from __future__ import annotations

import json
from typing import Any

from opsagent.core.config import settings
from opsagent.core.graph.state import ToolCallRecord

# 给 LLM 看时保留的关键字段（不含 result 大文本，result 单独截断处理）
TOPK_KEYS: tuple[str, ...] = ("tool_name", "args", "success", "latency_ms")


def _truncate(text: str, max_chars: int) -> str:
    """超长文本截断并标注，避免大块 result 撑爆上下文。"""
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"...(已截断, 原长 {len(text)})"


def compact_record_for_prompt(
    rec: ToolCallRecord, max_chars: int | None = None
) -> dict[str, Any]:
    """把一条工具调用记录压成精简 dict：Top-K 字段 + 截断后的 result。"""
    limit = max_chars if max_chars is not None else settings.tool_max_output_chars
    out: dict[str, Any] = {k: rec.get(k) for k in TOPK_KEYS}
    if rec.get("error"):
        out["error"] = rec["error"]
    result = rec.get("result")
    if result is not None:
        # result 可能是 dict/list/str，统一序列化后按字符上限截断
        result_repr = (
            result if isinstance(result, str)
            else json.dumps(result, ensure_ascii=False)
        )
        out["result"] = _truncate(result_repr, limit)
    return out


def compact_working_memory(
    wm: list[ToolCallRecord] | None, max_chars: int | None = None
) -> list[dict[str, Any]]:
    """批量压缩整轮工作记忆，供落库（episodic.tool_calls）或拼 prompt 用。"""
    if not wm:
        return []
    return [compact_record_for_prompt(rec, max_chars) for rec in wm]
