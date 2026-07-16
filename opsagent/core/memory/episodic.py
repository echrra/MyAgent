"""L3 Episodic 记忆 —— 单会话对话历史的装配与落库。

职责（docs/04 L3）:
- load_episodic : 从 episodic_summaries（压缩摘要）+ episodic_turns（最近 N 轮原文）
  装配 EpisodicContext。摘要在前、原文在后，贴合「越早越压缩、越近越完整」。
- persist_turn  : 把本轮 user / assistant 两行落库（一轮两行），轮号在同一连接内
  由 max_turn + 1 现算，避免多读一次库 / 竞态。
- EpisodicContext.to_messages : 转成 OpenAI messages 风格，供节点渲染进 prompt
  （也为「未来直接当历史消息喂模型」留口子）。

降级哲学（对齐 search_sop / W4-A）:
任一步 DB / 连接异常都只 logger.warning，返回空上下文 / 落库失败返回 None，
主链路退回「无历史」单轮行为，绝不让 memory 故障冒泡进 graph。

压缩摘要的填充是 W4-C 的事；本模块对 summaries 为空的情形天然兼容。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from loguru import logger

from opsagent.core.config import settings
from opsagent.core.memory import store

# prompt 渲染时的角色中文标签
_ROLE_LABEL = {"user": "用户", "assistant": "助手", "system": "摘要"}


@dataclass
class EpisodicContext:
    """单会话的对话上下文：压缩摘要 + 最近若干轮原文。"""

    summaries: list[dict[str, Any]] = field(default_factory=list)
    recent_turns: list[dict[str, Any]] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not self.summaries and not self.recent_turns

    def to_messages(self) -> list[dict[str, Any]]:
        """转成 messages 列表：摘要在前（system 角色），最近轮原文在后（按 turn,role 已正序）。"""
        msgs: list[dict[str, Any]] = []
        for s in self.summaries:
            msgs.append(
                {
                    "role": "system",
                    "content": (
                        f"【历史摘要 轮{s.get('covers_start')}-{s.get('covers_end')}】"
                        f"{s.get('summary')}"
                    ),
                }
            )
        for t in self.recent_turns:
            msgs.append({"role": t["role"], "content": t["content"]})
        return msgs


def render_history(messages: list[dict[str, Any]] | None) -> str:
    """把 episodic messages 渲染成 prompt 里好读的多行文本。

    空历史（本会话首轮）给一句占位，避免 prompt 出现空段落。
    """
    if not messages:
        return "（本会话第一轮，暂无历史对话）"
    lines = []
    for m in messages:
        role = m.get("role", "")
        label = _ROLE_LABEL.get(role, role)
        lines.append(f"{label}：{m.get('content', '')}")
    return "\n".join(lines)


def load_episodic(session_id: str, keep_recent_turns: int | None = None) -> EpisodicContext:
    """装配指定会话的历史上下文；DB 不可用则退回空上下文（不抛）。"""
    keep = (
        keep_recent_turns
        if keep_recent_turns is not None
        else settings.memory_keep_recent_turns
    )
    try:
        from opsagent.core.retrieval.store import connect

        with connect() as conn:
            summaries = store.load_summaries(conn, session_id)
            recent = store.load_recent_turns(conn, session_id, keep)
        return EpisodicContext(summaries=summaries, recent_turns=recent)
    except Exception as exc:
        logger.warning(
            f"[load_episodic] 装配历史失败，退回空上下文: {type(exc).__name__}: {exc}"
        )
        return EpisodicContext()


def persist_turn(
    session_id: str,
    user_content: str,
    assistant_content: str,
    tool_calls_json: str | None = None,
) -> int | None:
    """落库本轮对话（一轮两行）；轮号在同连接内现算。失败返回 None（不抛）。

    tool_calls_json 挂在 assistant 行（本轮工具调用的精简快照，供后续压缩/复盘）。
    """
    try:
        from opsagent.core.retrieval.store import connect

        with connect() as conn:
            turn = store.max_turn(conn, session_id) + 1
            store.append_turn(conn, session_id, turn, "user", user_content)
            store.append_turn(
                conn, session_id, turn, "assistant", assistant_content, tool_calls_json
            )
        return turn
    except Exception as exc:
        logger.warning(
            f"[persist_turn] 落库失败（不影响本轮响应）: {type(exc).__name__}: {exc}"
        )
        return None
