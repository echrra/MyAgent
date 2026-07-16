"""L2 Profile 记忆 —— 跨会话的用户画像 + 历史故障模式（docs/04 L2）。

两部分:
- 结构化画像（profile 表）: role / 技术栈 / 长期偏好等，**多版本不覆盖**。
- 历史故障模式（profile_fault_patterns 表）: 向量检索，按 query 召回相似历史经验。

冲突处理（docs/04 §5）:
不做「硬覆盖 / 硬选」，而是多版本 + 时间戳全量入库；装配时 to_prompt_block 把同一字段的
多个版本（最新在前、附时间戳）都摆给 LLM，让模型在上下文里「看到冲突」自行判断，
而不是代码替它决定哪个对。

降级哲学（对齐 W4-A/B/C）:
- load_profile: DB 不可用退空画像；向量召回（需 embedder）单独 try，模型不可用只丢故障模式、
  不影响结构化画像。
- run_profile_updater: 会话结束的异步抽取，全程吞异常，绝不影响主链路。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from itertools import groupby
from typing import Any

from loguru import logger

from opsagent.core.config import settings
from opsagent.core.llm.client import chat
from opsagent.core.memory import store
from opsagent.core.prompts import load as load_prompt


def _fmt_time(ts: Any) -> str:
    """时间戳渲染成 YYYY-MM-DD；拿不到就给占位。"""
    try:
        return ts.strftime("%Y-%m-%d")
    except Exception:
        return "?"


def _loads_json(text: str) -> dict[str, Any]:
    """LLM 偶尔用 ```json 包裹，清洗后解析（与 compactor 同款，避免跨层依赖）。"""
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines)
    return json.loads(text)


@dataclass
class ProfileContext:
    """单用户的画像上下文：结构化字段多版本 + 召回的历史故障模式。"""

    # versions 已按 (field ASC, created_at DESC) 排好（store.load_profile_versions 保证）
    versions: list[dict[str, Any]] = field(default_factory=list)
    fault_patterns: list[dict[str, Any]] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not self.versions and not self.fault_patterns

    @property
    def role(self) -> str | None:
        """取最新的 role 字段值（versions 已最新优先），无则 None。"""
        for v in self.versions:
            if v["field"] == "role":
                return v["value"]
        return None

    def to_prompt_block(self) -> str:
        """渲染成 prompt 块；同字段多版本全列（冲突可见），附历史故障模式。"""
        if self.is_empty():
            return "（暂无用户画像）"
        lines: list[str] = []
        if self.versions:
            lines.append("【用户画像】")
            # versions 已按 field 排序，groupby 可直接按字段聚合
            for fname, grp in groupby(self.versions, key=lambda v: v["field"]):
                vs = list(grp)
                newest = vs[0]
                line = (
                    f"- {fname}：{newest['value']}"
                    f"（{_fmt_time(newest['created_at'])}，{newest['source']}）"
                )
                if len(vs) > 1:
                    # 多版本 = 冲突，把旧值也摆出来让 LLM 自行权衡
                    hist = "；".join(
                        f"{v['value']}（{_fmt_time(v['created_at'])}）" for v in vs[1:]
                    )
                    line += f" ｜ 历史值（可能已过时，供参考）：{hist}"
                lines.append(line)
        if self.fault_patterns:
            lines.append("【历史故障模式（相似经历，仅供参考）】")
            for fp in self.fault_patterns:
                lines.append(f"- [距离 {fp['dist']:.3f}] {fp['summary']}")
        return "\n".join(lines)


def load_profile(
    user_id: str, query: str | None = None, top_n: int | None = None
) -> ProfileContext:
    """装配用户画像：结构化字段全量 + （可选）按 query 向量召回历史故障模式。

    DB 不可用退空画像；向量召回失败只丢故障模式，不影响结构化画像。全程不抛。
    """
    n = top_n if top_n is not None else settings.memory_fault_pattern_top_n
    versions: list[dict[str, Any]] = []
    fault_patterns: list[dict[str, Any]] = []
    try:
        from opsagent.core.retrieval.store import connect

        with connect() as conn:
            versions = store.load_profile_versions(conn, user_id)
            if query:
                try:
                    from opsagent.core.retrieval.embedder import get_embedder

                    vec = get_embedder().embed_query(query)
                    fault_patterns = store.search_fault_patterns(conn, user_id, vec, n)
                except Exception as exc:
                    logger.warning(
                        f"[load_profile] 故障模式向量召回失败，跳过: {type(exc).__name__}: {exc}"
                    )
    except Exception as exc:
        logger.warning(
            f"[load_profile] 画像加载失败，退回空画像: {type(exc).__name__}: {exc}"
        )
    return ProfileContext(versions=versions, fault_patterns=fault_patterns)


async def run_profile_updater(user_id: str, conversation: str) -> dict[str, Any] | None:
    """会话结束的异步画像抽取：updater 别名从对话里抽稳定画像 + 历史故障模式并落库。

    fire-and-forget 语义：任一步失败只 logger.warning，返回 None，绝不影响主链路。
    返回抽取到的结构（便于手动调用 / 测试断言）。
    """
    if not user_id:
        return None
    try:
        prompt = load_prompt("profile_updater").format(conversation=conversation)
        resp = await chat(
            alias="updater",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0,
            max_tokens=400,
        )
        data = _loads_json(resp["choices"][0]["message"]["content"])

        def _write() -> None:
            from opsagent.core.retrieval.store import connect

            with connect() as conn:
                # 结构化字段：多版本不覆盖
                for f in data.get("fields", []) or []:
                    if f.get("field") and f.get("value"):
                        store.upsert_profile_field(
                            conn, user_id, str(f["field"]), str(f["value"]), source="updater"
                        )
                # 历史故障模式：向量化后入库（embedder 不可用则跳过这部分）
                fp = data.get("fault_pattern")
                if fp and fp.get("summary"):
                    from opsagent.core.retrieval.embedder import get_embedder

                    vec = get_embedder().embed_query(fp["summary"])
                    store.insert_fault_pattern(conn, user_id, fp["summary"], vec)

        import asyncio

        await asyncio.to_thread(_write)
        logger.info(
            f"[profile_updater] user={user_id} 抽取 {len(data.get('fields') or [])} 字段 "
            f"故障模式={'有' if data.get('fault_pattern') else '无'}"
        )
        return data
    except Exception as exc:
        logger.warning(
            f"[profile_updater] 抽取/写入失败（忽略）: {type(exc).__name__}: {exc}"
        )
        return None
