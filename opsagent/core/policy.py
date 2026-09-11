"""阶段化权限门 —— 工具执行前检查「当前阶段有没有权限调这个工具」。

为什么需要这层：
OpsAgent 具备「致命三角」的全部三个条件：
  ① 访问私有数据（tls_client.py 拉真实生产日志）
  ② 处理不可信内容（日志文本可能含注入指令）
  ③ 外部通信能力（模型经代理调用、create_ticket 创建工单）
当前防御只有 pii_sanitizer.py。权限门补的是「工具执行层」的硬拦截：
不管 LLM 想调什么，代码层说不行就是不行。

设计原则：
  - 白名单制：每个阶段（节点）明确允许的工具集，不在列表的一律拒绝
  - 旁路防护：不改动业务逻辑（nodes.py 的调用链路不变），只在执行前拦截
  - 全关开关：policy_enabled=False 时完全旁路，恢复到无门状态
  - 审计日志：每次拦截都落 log，供后续分析

阶段定义（与 graph builder 节点一一对应）：
  load_memory   → 不允许任何工具调用（纯上下文装配）
  coordinator   → 仅元数据工具（不应在假设生成阶段就查日志）
  worker        → 查询类工具（诊断主体，可调所有 read 类工具）
  synthesizer   → 不允许任何工具调用（纯文本综合）
  tool_exec     → 查询类工具（v1 ReAct 回退路径）

使用方式：
  在 _exec_tool 中（nodes.py），拿到 state 后判断 caller_phase:
    from opsagent.core.policy import check_tool_permission
    allowed, reason = check_tool_permission(phase, tool_name, tool)
    if not allowed:
        return ToolCallRecord with error=f"policy_denied: {reason}"
"""

from __future__ import annotations

from typing import Any

from loguru import logger

from opsagent.core.tools.base import Tool

# ====================== 白名单矩阵 ======================

# 每个阶段允许调用的工具名集合。不在列表的工具 → 拦截 + 审计。
# 原则：最小权限 —— 每个阶段只给它完成职责所必需的工具。
_PHASE_WHITELIST: dict[str, set[str]] = {
    # load_memory: 纯 DB IO 装配上下文，不经过工具体系
    "load_memory": set(),

    # coordinator: 只产假设，不调工具（v2 coordinator 节点本身不直接调工具，
    # 但如果被 prompt 注入诱导，LLM 可能在 plan 输出里塞 tool_call，
    # 这里的白名单就是个兜底防线）
    "coordinator": {
        "search_sop",    # 查 SOP 元数据（帮助生成假设）
        "kb_search",     # MCP 等价物
        "wiki_read",     # 读知识库目录
    },

    # worker: 诊断主体，可调所有查询类工具
    "worker": {
        "search_logs", "search_sop", "kb_search", "wiki_read",
        "get_service_metrics", "query_metrics", "trace_query",
        "change_query",
        # create_ticket 不在此列 —— 工单创建只能由用户显式触发，worker 无权
    },

    # synthesizer: 纯综合文本，不接触外部世界
    "synthesizer": set(),

    # tool_exec (v1 ReAct 回退): 允许查询类工具，同 worker
    "tool_exec": {
        "search_logs", "search_sop", "kb_search", "wiki_read",
        "get_service_metrics", "query_metrics", "trace_query",
        "change_query",
    },

    # plan (v1 ReAct 决策): 不直接调工具，但 _exec_tool 复用入口
    "plan": set(),
}


# ====================== 权限检查 ======================


def check_tool_permission(
    phase: str,
    tool_name: str,
    tool: Tool | None = None,
    *,
    enabled: bool = True,
) -> tuple[bool, str]:
    """检查指定阶段是否有权限调用指定工具。

    Args:
        phase: 当前执行阶段（"coordinator" / "worker" / "synthesizer" / "tool_exec" 等）
        tool_name: 要调用的工具名
        tool: Tool 实例（可选，用于检查 risk/side_effect 元数据）
        enabled: 门禁开关；False 时完全旁路不拦截

    Returns:
        (allowed, reason)
        - allowed: True 表示放行
        - reason: 拒绝原因（allowed=True 时为空串）
    """
    if not enabled:
        return True, ""

    # 1. 白名单检查
    allowed_tools = _PHASE_WHITELIST.get(phase)
    if allowed_tools is None:
        # 未知阶段：安全起见拒绝（宁可误拦不可放行）
        logger.warning(f"[policy] 未知阶段 '{phase}'，拒绝 {tool_name}")
        return False, f"unknown_phase: {phase}"

    if tool_name not in allowed_tools:
        logger.warning(
            f"[policy] 拦截 | 阶段={phase} 工具={tool_name} | "
            f"原因: 不在该阶段白名单中（白名单: {sorted(allowed_tools) or '空'}）"
        )
        return False, f"tool_not_in_whitelist: phase={phase} tool={tool_name}"

    # 2. 有 Tool 实例时，检查风险等级约束
    if tool is not None:
        # coordinator 阶段不允许调有副作用的工具（防御纵深：即使白名单里有
        # 写工具，side_effect 检查也能拦住）
        if tool.side_effect and phase in ("coordinator", "synthesizer", "plan"):
            logger.warning(
                f"[policy] 拦截 | 阶段={phase} 工具={tool_name} | "
                f"原因: 有副作用工具 (side_effect=True) 不允许在 {phase} 阶段调用"
            )
            return False, (
                f"side_effect_denied: tool={tool_name} "
                f"phase={phase} risk={tool.risk}"
            )

    return True, ""


# ====================== 审计 ======================


def get_whitelist_summary() -> dict[str, list[str]]:
    """返回当前白名单矩阵摘要（供调试 / 文档生成）。"""
    return {phase: sorted(tools) for phase, tools in _PHASE_WHITELIST.items()}


def format_whitelist_table() -> str:
    """生成白名单矩阵的 Markdown 表格（供报告/文档嵌入）。"""
    all_tools = sorted({
        tool for tools in _PHASE_WHITELIST.values() for tool in tools
    })
    phases = sorted(_PHASE_WHITELIST.keys())

    lines = ["| 工具 \\ 阶段 | " + " | ".join(phases) + " |"]
    lines.append("|---|" + "---|" * len(phases))
    for tool in all_tools:
        row = [tool]
        for phase in phases:
            allowed = tool in _PHASE_WHITELIST.get(phase, set())
            row.append("✅" if allowed else "❌")
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)
