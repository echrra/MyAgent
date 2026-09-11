"""L1: 纯 gate 矩阵测试。

不走 graph,不调 LLM,直接对 _PHASE_WHITELIST × DANGEROUS_TOOLS 做笛卡尔积,
验证 check_tool_permission 在每个阶段对危险工具都返回拒绝。

跑这个测试能发现的问题:
- 某个 phase 漏配(不在白名单矩阵里)
- 危险工具被意外加进了某个 phase 的白名单
- side_effect 检查失效
"""

from __future__ import annotations

from typing import Any


def run_matrix_tests() -> dict[str, Any]:
    """跑 L1 矩阵测试,返回结果摘要。

    Returns:
        {
            "total": 总共测了多少 (phase, tool) 组合,
            "passed": 通过数,
            "failed": 失败数,
            "failures": [(phase, tool, reason), ...]
        }
    """
    # 延迟 import,避免顶层就加载
    from opsagent.core.policy import _PHASE_WHITELIST, check_tool_permission

    from gate_harness_test.dangerous_tools import DANGEROUS_TOOLS

    total = 0
    passed = 0
    failures: list[tuple[str, str, str]] = []

    # 对每个 phase × 每个危险工具,验证 gate 是必须拦截的
    for phase, whitelist in _PHASE_WHITELIST.items():
        for tool_name, tool_obj in DANGEROUS_TOOLS.items():
            total += 1
            allowed, reason = check_tool_permission(phase, tool_name, tool_obj)
            if allowed:
                failures.append(
                    (phase, tool_name, f"gate 未拦截 phase={phase} tool={tool_name}")
                )
            else:
                passed += 1

    return {
        "total": total,
        "passed": passed,
        "failed": len(failures),
        "failures": failures,
    }


def format_matrix_report(result: dict[str, Any]) -> str:
    """生成 L1 测试报告。"""
    lines = [
        "## L1 Gate 矩阵测试",
        "",
        f"- 总测试数: {result['total']}",
        f"- 通过: {result['passed']}",
        f"- 失败: {result['failed']}",
        "",
    ]
    if result["failures"]:
        lines.append("### 失败项")
        for phase, tool, reason in result["failures"]:
            lines.append(f"- ❌ `{phase}` × `{tool}`: {reason}")
    else:
        lines.append("✅ 所有 (phase × dangerous_tool) 组合都被 gate 正确拦截")
    return "\n".join(lines)
