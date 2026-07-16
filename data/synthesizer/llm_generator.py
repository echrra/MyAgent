"""
LLM Generator —— 把 Rule 注入的"骨架行"批量交给 DeepSeek 仿写为自然日志。

输入：list[LogSkeleton]（msg / Content 字段为 <FILL_BY_LLM>）
输出：list[dict] —— jsonl 行，msg/Content 已被仿写覆盖

设计要点：
    1. 批量调用：默认每 8 行打包成 1 次 LLM 请求，节省 token 与 RTT
    2. 严格 JSON 输出：用 system prompt 强约束，response_format 走 json_object
    3. Few-shot 范例放在 system 末尾，由 schema.<svc>.yaml 抽出 + 故障短语 hint 拼成
    4. 失败容错：JSON 解析失败 → 该批次整体回退为骨架原文（msg 字段保留 phrase_hint）
       —— 这种"硬伤"在端到端跑通后再考虑加重试。

与业务 Agent 节点 (plan/reflect/answer) 完全解耦：用别名 "generator"。
"""
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any

import yaml
from loguru import logger

from opsagent.core.llm.client import chat

from .fault_patterns.base import FILL_PLACEHOLDER, LogSkeleton

# 单次 LLM 请求填充的行数；超过此数量自动分批
DEFAULT_BATCH_SIZE = 8


_SYSTEM_PROMPT = """\
你是一个日志合成助手，负责把"骨架行"补全成一行符合 GoFrame glog 风格的真实日志文本。

约束：
1. 对输入数组里每个对象，输出一个新对象，仅包含被填的字段：`msg`（必填）和 `content`（仅当输入里 need_content=true 时才填）。
2. `msg` 是一句简洁的英文动词短语（≤ 25 词），必须自然贴合 `phrase_hint`。
   - phrase_hint 含 `<ULID>` / `<SN>` / `<HOST>` / `<TOKEN>` / `<DB>` / `<TABLE>` / `<KEY>` / `<N>` / `<CODE>` 等占位符时，可以替换为合理占位值（如 `01ABCDEFGHJKMNPQRSTVWXYZ00` / `X000000000001` / `service-a.internal` / `db_main` / `users`），**严禁写真实业务数据或真实公司名**。
3. `content` 是错误日志多行体，格式：
   `Error: <错误描述>\\nStack:\\n  <frame_1>\\n  <frame_2>\\n  <frame_3>`（3 行起步）
   - 如果输入提供 `stack_template_hint`，content 里的 Stack 部分必须严格贴合该模板的层级数量和文件路径风格，不要凭空发明栈帧。
   - 模板中的占位符（<N> / <HEX> / <err> 等）替换为合理示意值（如 116 / 0xc / "context canceled"）。
4. `msg` 与 `phrase_hint` 应紧密呼应，不要换说法；如果 phrase_hint 是完整短语，msg 应包含其主干。
5. 严禁写 `[ERRO]` / `[INFO]` 这种级别前缀——级别由 Level 字段控制。
6. 整体输出必须是合法 JSON：{"results": [{"msg": "...", "content": "..."}, ...]} —— 顺序与输入一致，不允许多/少。
"""


# few-shot 范例 —— 故意贴近 F1 级联超时模式，让模型有"模仿对象"
_FEW_SHOT_USER = """\
[
  {"level": "ERRO", "path": "/api/v1/work/deploy", "status_code": 504, "phrase_hint": "context deadline exceeded", "need_content": true},
  {"level": "INFO", "path": "/api/v1/work/deploy", "status_code": 200, "phrase_hint": "request completed", "need_content": false}
]"""

_FEW_SHOT_ASSISTANT = """\
{"results": [
  {"msg": "failed to deploy work 01ABCDEFGHJKMNPQRSTVWXYZ00: context deadline exceeded", "content": "Error: rpc call to upstream timeout after 5s\\nStack:\\n  1. service/deploy.go:142\\n  2. controller/work.go:88\\n  3. middleware/trace.go:34"},
  {"msg": "request completed"}
]}"""


class LLMGenerator:
    """批量填充骨架日志的 msg / Content。

    用法：
        gen = LLMGenerator()
        rows = await gen.fill(skeletons)
    """

    def __init__(
        self,
        alias: str = "generator",
        batch_size: int = DEFAULT_BATCH_SIZE,
        schema_yaml: Path | None = None,
    ) -> None:
        """
        Args:
            alias: LiteLLM 别名（默认 generator → DeepSeek）
            batch_size: 单次 LLM 调用填充的行数（越大越省 token，但 JSON 容错越脆）
            schema_yaml: schema.<svc>.yaml 路径（可选；用于增强 system prompt 中的字段约束）
        """
        self.alias = alias
        self.batch_size = batch_size
        # schema yaml 暂时只在初始化时打印一下加载情况；后续若要把字段约束塞进 system，再用
        if schema_yaml and schema_yaml.exists():
            with schema_yaml.open("r", encoding="utf-8") as f:
                self.schema = yaml.safe_load(f)
            logger.debug("Generator 加载 schema: {}", schema_yaml.name)
        else:
            self.schema = None

        # 计数器（合成结束后由 run_all 打印汇总）
        self.stats = {
            "batches_total": 0,
            "batches_failed": 0,
            "rows_filled": 0,
            "rows_fallback": 0,   # JSON 解析失败 → 用 hint 兜底
        }

    # ------------------------------------------------------------------
    # 对外主入口
    # ------------------------------------------------------------------
    async def fill(self, skeletons: list[LogSkeleton]) -> list[dict[str, Any]]:
        """批量填充骨架，返回 jsonl-ready 字典列表。

        顺序保持与输入一致；填充失败的行用 phrase_hint 兜底（msg = hint）。
        """
        if not skeletons:
            return []

        # 分批处理
        batches: list[list[LogSkeleton]] = []
        for i in range(0, len(skeletons), self.batch_size):
            batches.append(skeletons[i : i + self.batch_size])

        # 并发 LLM 调用，节省总耗时（DeepSeek 单次 ~2s）
        coros = [self._fill_one_batch(b) for b in batches]
        batch_results = await asyncio.gather(*coros, return_exceptions=False)

        # 平铺 + 顺序保留
        out: list[dict[str, Any]] = []
        for batch_filled in batch_results:
            out.extend(batch_filled)
        return out

    # ------------------------------------------------------------------
    # 私有：单批次填充
    # ------------------------------------------------------------------
    async def _fill_one_batch(self, batch: list[LogSkeleton]) -> list[dict[str, Any]]:
        self.stats["batches_total"] += 1

        # 构造 LLM 输入：只保留 LLM 需要看的字段，骨架本身的其它字段稍后回填
        prompt_items = [
            {
                "level": s.level,
                "path": s.path,
                "status_code": s.status_code,
                "phrase_hint": s.fault_phrase_hint or "request completed",
                "need_content": s.content == FILL_PLACEHOLDER,
                "stack_template_hint": s.stack_template_hint,    # 可能为 None
            }
            for s in batch
        ]

        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": _FEW_SHOT_USER},
            {"role": "assistant", "content": _FEW_SHOT_ASSISTANT},
            {"role": "user", "content": json.dumps(prompt_items, ensure_ascii=False)},
        ]

        try:
            resp = await chat(
                alias=self.alias,
                messages=messages,
                response_format={"type": "json_object"},
                # 限长足够覆盖 batch_size=8 + 每行 ~300 token
                max_tokens=2048,
            )
            text = resp["choices"][0]["message"]["content"]
            parsed = self._parse_results(text, expected=len(batch))
        except Exception as exc:
            logger.warning("Generator batch 失败 ({}): {}", type(exc).__name__, exc)
            self.stats["batches_failed"] += 1
            parsed = None

        if parsed is None or len(parsed) != len(batch):
            # 兜底：把 phrase_hint 直接当 msg
            logger.warning("Generator 批次回退到 hint 兜底 (size={})", len(batch))
            self.stats["rows_fallback"] += len(batch)
            return [self._fallback_row(s) for s in batch]

        # 正常路径：把 LLM 填好的 msg/content 回填到骨架并落 dict
        self.stats["rows_filled"] += len(batch)
        rows: list[dict[str, Any]] = []
        for skel, filled in zip(batch, parsed, strict=True):
            d = skel.to_jsonl_dict()
            d["msg"] = filled.get("msg", skel.fault_phrase_hint or "").strip() or skel.msg
            if skel.content == FILL_PLACEHOLDER and "content" in filled:
                d["Content"] = filled["content"]
            elif skel.content == FILL_PLACEHOLDER:
                # LLM 漏填 content：写一个简短占位
                d["Content"] = f"Error: {d['msg']}\nStack:\n  1. <unknown>:0"
            rows.append(d)
        return rows

    # ------------------------------------------------------------------
    # 私有：LLM 响应解析（尽力容错）
    # ------------------------------------------------------------------
    @staticmethod
    def _parse_results(text: str, expected: int) -> list[dict[str, Any]] | None:
        """提取 JSON 数组；deepseek 偶尔会用 ```json 包裹，做 1 次清洗。"""
        cleaned = text.strip()
        # 去 markdown 围栏
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
            cleaned = re.sub(r"\s*```$", "", cleaned)
        try:
            obj = json.loads(cleaned)
        except json.JSONDecodeError:
            return None
        results = obj.get("results")
        if not isinstance(results, list):
            return None
        if len(results) != expected:
            return None
        return results

    # ------------------------------------------------------------------
    # 私有：兜底行
    # ------------------------------------------------------------------
    @staticmethod
    def _fallback_row(skel: LogSkeleton) -> dict[str, Any]:
        d = skel.to_jsonl_dict()
        # 兜底 msg：直接用 phrase_hint
        d["msg"] = skel.fault_phrase_hint or "request completed"
        if skel.content == FILL_PLACEHOLDER:
            d["Content"] = f"Error: {d['msg']}\nStack:\n  1. <unknown>:0"
        return d
