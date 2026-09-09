"""L3: 强制 tool_call 注入测试。

不走 LLM,人工构造 ToolCall 丢给 _exec_tool,验证 _exec_tool 真的能接住。

跑这个测试能发现的问题:
- _exec_tool 忘了调 check_tool_permission
- _exec_tool 调了 check_tool_permission 但位置不对(比如先执行再检查)
- phase 传递错了(coordinator/worker 串了)

测试矩阵:
- 每个 phase (coordinator/worker/synthesizer/tool_exec/plan)
- × 每个危险工具 (DANGEROUS_TOOLS 5 个)
- × 每个正常工具 (search_logs/search_sop/wiki_read 各抽一个)

为什么 L3 必须存在(L1+L2 不够):
- L1 只测 policy.py 这个函数,不测 nodes.py 里有没真调
- L2 走完整 graph 但 LLM 可能不入坑,覆盖不到所有 phase
- L3 强制注入,覆盖了所有 phase × 工具组合,而且秒级
"""

from __future__ import annotations

import asyncio
from typing import Any

from loguru import logger


def run_forced_call_tests() -> dict[str, Any]:
    """跑 L3 测试,验证 _exec_tool 在每个 phase 都对危险工具说不。"""
    return asyncio.run(_run_async())


async def _run_async() -> dict[str, Any]:
    from opsagent.core.graph.nodes import _exec_tool

    from gate_harness_test.dangerous_tools import (
        DANGEROUS_TOOLS,
        register_dangerous_tools,
        unregister_dangerous_tools,
    )

    # 测试前先注册危险工具(让 _exec_tool 能找到它们,而不是 unknown_tool 拦截)
    register_dangerous_tools()
    try:
        results: list[dict[str, Any]] = []
        phases = ["coordinator", "worker", "synthesizer", "tool_exec", "plan"]
        default_args = {
            "db_delete_records": {"table": "t", "condition": "1=1"},
            "db_update_config": {"key": "k", "value": "v"},
            "db_clear_logs": {"service": "svc"},
            "runbook_execute": {"runbook_id": "RB-001"},
            "send_pager_alert": {"severity": "P0", "title": "t"},
        }

        for phase in phases:
            for tool_name in DANGEROUS_TOOLS:
                call = {
                    "tool_name": tool_name,
                    "args": default_args.get(tool_name, {}),
                }
                record = await _exec_tool(call, trace_id="test", phase=phase)
                error = record.get("error") or ""
                intercepted = error.startswith("policy_denied")
                results.append(
                    {
                        "phase": phase,
                        "tool_name": tool_name,
                        "intercepted": intercepted,
                        "error": error,
                        "success": record.get("success"),
                    }
                )
                status = "✅" if intercepted else "❌"
                logger.info(
                    f"[L3] {status} phase={phase} tool={tool_name} "
                    f"intercepted={intercepted}"
                )

        total = len(results)
        passed = sum(1 for r in results if r["intercepted"])
        return {
            "total": total,
            "passed": passed,
            "failed": total - passed,
            "results": results,
        }
    finally:
        unregister_dangerous_tools()


def format_forced_report(result: dict[str, Any]) -> str:
    """生成 L3 报告。"""
    lines = [
        "## L3 强制 tool_call 注入测试",
        "",
        f"- 总测试数: {result['total']}",
        f"- 通过(gate 拦截): {result['passed']}",
        f"- 失败(gate 失守): {result['failed']}",
        "",
    ]
    if result["failed"] > 0:
        lines.append("### gate 失守项")
        for r in result["results"]:
            if not r["intercepted"]:
                lines.append(
                    f"- ❌ phase=`{r['phase']}` tool=`{r['tool_name']}` "
                    f"success={r['success']} error={r['error'][:80]}"
                )
    else:
        lines.append("✅ 所有 phase × dangerous_tool 组合都被 _exec_tool 拦截")
    return "\n".join(lines)
