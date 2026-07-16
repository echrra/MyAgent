"""L3 Episodic 压缩引擎 —— 上下文超阈值时把早期对话压成结构化摘要（docs/04 §压缩）。

核心取舍:
- 触发判断 should_compact 是纯函数（manual / 轮数 / token 比例三选一），可脱离 DB / LLM 单测。
- 压缩「LLM 回答 + 工具长输出」，用户原始诉求不丢；摘要为 6 字段结构化 JSON，
  非自由文本，便于后续轮次快速回顾，也便于按字段检索回填。
- 「原始不丢」：压缩只是「往 summaries 表新增摘要 + 旧低层摘要标 superseded」，
  episodic_turns 原文一行不删；装配时用摘要替代早期原文进 prompt，但 DB 仍可回溯。
- 递归压缩：一级摘要累计达阈值（memory_summary_merge_count）→ 合并为更高层摘要，
  被合并的低层标 superseded（不删）。
- 失败兜底：LLM / DB 任一步异常 → logger.warning + 返回 None，绝不删原文、绝不冒泡。

token 计数:
- tiktoken（cl100k_base）做 token 估算；Qwen 无公开 tokenizer，用通用 BPE 近似，
  0.7 比例阈值是启发式，不需精确。
- 上下文窗口恒取 settings.memory_max_context_tokens（自定义别名无法从 SDK 获取上限）。
"""

from __future__ import annotations

import json
from typing import Any

from loguru import logger

from opsagent.core.config import settings
from opsagent.core.llm.client import chat
from opsagent.core.memory import store
from opsagent.core.memory.episodic import render_history
from opsagent.core.prompts import load as load_prompt

# 仅用于 token「估算」的通用 tokenizer（Qwen 无公开 tokenizer，用通用 BPE 近似，
# 阈值是启发式，不追求精确）。不是业务可调配置，故作模块常量而非进 settings。
_TOKENIZER_MODEL = "gpt-3.5-turbo"
_encoding = None


def _get_encoding():
    """懒加载 tiktoken encoder，失败返回 None。"""
    global _encoding
    if _encoding is None:
        try:
            import tiktoken
            _encoding = tiktoken.encoding_for_model(_TOKENIZER_MODEL)
        except Exception:
            pass
    return _encoding


# ====================== 触发判断（纯函数，可单测）======================

def should_compact(
    *,
    n_turns: int,
    context_tokens: int,
    max_context_tokens: int | None = None,
    manual: bool = False,
) -> tuple[bool, str]:
    """是否触发压缩，返回 (是否, 原因)。三选一：手动 / 轮数超限 / token 比例超阈值。"""
    if manual:
        return True, "manual"
    if n_turns >= settings.memory_compact_round_count:
        return True, f"rounds>={settings.memory_compact_round_count}"
    max_ctx = max_context_tokens or settings.memory_max_context_tokens
    if max_ctx > 0 and context_tokens / max_ctx >= settings.memory_compact_token_ratio:
        return True, f"token_ratio>={settings.memory_compact_token_ratio}"
    return False, "no"


def count_tokens(messages: list[dict[str, Any]], model_id: str = _TOKENIZER_MODEL) -> int:
    """估算 messages 的 token 数；tiktoken 不可用时退字符数粗估。"""
    enc = _get_encoding()
    if enc is not None:
        try:
            total = 0
            for m in messages:
                total += len(enc.encode(str(m.get("content", ""))))
            return total
        except Exception:
            pass
    # 兜底：中文约 1.5-2 字符/token，取 //2 偏保守（宁可早触发压缩）
    return sum(len(str(m.get("content", ""))) for m in messages) // 2


def resolve_max_context(model_id: str | None = None) -> int:
    """主模型上下文窗口；自定义别名无法从 SDK 获取上限，直接返回 settings 兜底值。"""
    return settings.memory_max_context_tokens


# ====================== JSON 清洗（与 nodes 同款，避免反向依赖 graph 层）======================

def _loads_json(text: str) -> dict[str, Any]:
    """LLM 偶尔用 ```json 包裹，清洗后解析。"""
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines)
    return json.loads(text)


async def _summarize_chunk(chunk: str) -> dict[str, Any]:
    """调 updater 别名把一段对话/摘要压成 6 字段结构化摘要（失败抛，由上层兜底）。"""
    prompt = load_prompt("compact").format(conversation_chunk=chunk)
    resp = await chat(
        alias="updater",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.0,
        max_tokens=600,
    )
    content = resp["choices"][0]["message"]["content"]
    return _loads_json(content)


# ====================== 压缩主流程 ======================

async def compact_session(session_id: str, reason: str = "manual") -> dict[str, Any] | None:
    """压缩指定会话的「早期、尚未被摘要覆盖」的轮次。

    流程: 算待压缩区间 → updater 生成一级摘要落库 → 触发递归合并 → 返回摘要。
    任一步异常 → 保留原文不动、返回 None。
    """
    from opsagent.core.retrieval.store import connect

    keep = settings.memory_keep_recent_turns
    try:
        # 1. 算待压缩区间：[已覆盖末尾+1, 最大轮号-保留轮数]
        def _read() -> tuple[int, int, list[dict[str, Any]]]:
            with connect() as conn:
                max_t = store.max_turn(conn, session_id)
                summaries = store.load_summaries(conn, session_id)
                covered = max(
                    (s["covers_end"] for s in summaries if s["level"] == 1), default=0
                )
                start = covered + 1
                end = max_t - keep
                turns = (
                    store.load_turns_range(conn, session_id, start, end)
                    if end >= start
                    else []
                )
            return start, end, turns

        start, end, turns = await _to_thread(_read)
        if not turns:
            logger.info(f"[compact] session={session_id} 无待压缩轮次，跳过")
            return None

        # 2. 渲染待压缩原文 → updater 生成结构化摘要
        chunk = render_history(
            [{"role": t["role"], "content": t["content"]} for t in turns]
        )
        summary = await _summarize_chunk(chunk)
        summary_json = json.dumps(summary, ensure_ascii=False)

        # 3. 落一级摘要
        def _write_l1() -> None:
            with connect() as conn:
                store.insert_summary(conn, session_id, 1, start, end, summary_json)

        await _to_thread(_write_l1)
        logger.info(
            f"[compact] session={session_id} 压缩轮 {start}-{end} 完成 (reason={reason})"
        )

        # 4. 递归合并（一级摘要够多 → 合并成更高层）
        await _maybe_merge_summaries(session_id)
        return summary
    except Exception as exc:
        logger.warning(
            f"[compact] 压缩失败，保留原始对话不动: {type(exc).__name__}: {exc}"
        )
        return None


async def _maybe_merge_summaries(session_id: str) -> None:
    """一级摘要累计达 memory_summary_merge_count → 合并为 level2，旧的标 superseded。"""
    from opsagent.core.retrieval.store import connect

    def _read_l1() -> list[dict[str, Any]]:
        with connect() as conn:
            return [s for s in store.load_summaries(conn, session_id) if s["level"] == 1]

    l1 = await _to_thread(_read_l1)
    if len(l1) < settings.memory_summary_merge_count:
        return

    merged_chunk = "\n".join(
        f"摘要(轮{s['covers_start']}-{s['covers_end']}): {s['summary']}" for s in l1
    )
    summary = await _summarize_chunk(merged_chunk)
    summary_json = json.dumps(summary, ensure_ascii=False)
    start = min(s["covers_start"] for s in l1)
    end = max(s["covers_end"] for s in l1)
    ids = [s["id"] for s in l1]

    def _write_l2() -> None:
        with connect() as conn:
            store.insert_summary(conn, session_id, 2, start, end, summary_json)
            store.mark_summaries_superseded(conn, ids)

    await _to_thread(_write_l2)
    logger.info(
        f"[compact] 递归合并 {len(l1)} 条一级摘要 → level2 覆盖轮 {start}-{end}"
    )


async def _to_thread(fn):
    """把同步 DB 操作丢线程池，避免阻塞事件循环（背景任务也不独占 loop）。"""
    import asyncio

    return await asyncio.to_thread(fn)
