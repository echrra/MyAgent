"""L2: LLM 诱导 + gate 联动测试。

走完整 graph,让 LLM 看到危险工具(临时注册到 TOOL_REGISTRY)、读到毒 SOP,
然后看:
- LLM 会不会被骗(attempted_dangerous=True)
- gate 能不能接住(error 以 policy_denied 开头)
- mock 工具的 fn 是否真的没被调

这是唯一需要 LLM API 的一层,跑得慢(3-5 min/条)。
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from loguru import logger

# 项目根目录加入 path(scripts/ 下运行时需要)
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from gate_harness_test.audit import (
    clear_current_scenario,
    end_audit_session,
    install_decision_hook,
    read_audit_logs,
    set_current_scenario,
    start_audit_session,
    uninstall_decision_hook,
)
from gate_harness_test.dangerous_tools import (
    register_dangerous_tools,
    unregister_dangerous_tools,
)
from gate_harness_test.mock_data.sop_switcher import (
    restore_normal_sop,
    switch_to_poisoned_sop,
)
from gate_harness_test.mock_data.role_switcher import (
    restore_default_role,
    switch_to_executor_role,
)
from gate_harness_test.verifiers import (
    verify_audit_complete,
    verify_dangerous_not_executed,
    verify_gate_denied,
    verify_llm_attempted_dangerous,
)

SCENARIOS_ROOT = Path(__file__).resolve().parent.parent / "scenarios"


def load_scenarios(category: str | None = None) -> list[dict[str, Any]]:
    """加载 scenarios 目录下的所有 yaml。category 非空则只加载该子目录。"""
    if category:
        pattern = f"{category}/*.yaml"
    else:
        pattern = "*/*.yaml"  # 所有子目录

    scenarios = []
    for path in sorted(SCENARIOS_ROOT.glob(pattern)):
        try:
            with path.open("r", encoding="utf-8") as fh:
                data = yaml.safe_load(fh)
            data["_path"] = str(path)
            scenarios.append(data)
        except Exception as exc:
            logger.error(f"加载 scenario 失败 {path}: {exc}")
    return scenarios


async def run_single_scenario(scenario: dict[str, Any]) -> dict[str, Any]:
    """跑单条 L2 情景。"""
    scenario_id = scenario.get("id", "?")
    query = scenario.get("query", "")
    test_config = scenario.get("test_config", {}) or {}
    expect = scenario.get("expect", {}) or {}

    logger.info(f"[L2] 开始 {scenario_id}: {scenario.get('title', '')}")

    # 1. 注册危险工具
    exposed = test_config.get("expose_dangerous_tools") or []
    registered = register_dangerous_tools(exposed)
    logger.info(f"[L2] 注册危险工具: {registered}")

    # 2. 切换 SOP 语料(如需)
    if test_config.get("use_poisoned_sop"):
        switch_to_poisoned_sop()

    # 2.5 切换 system_prompt 人格(X 系列 cross_role 专用)
    # 目的:绕过 OpsAgent 自带的 prompt 防线(严禁编造/只诊断),
    #       让 LLM 处于"executor 人格,直接听命令"的状态,把压力推到 gate
    if test_config.get("override_system_role") == "executor":
        switch_to_executor_role()

    # 3. 标记当前 scenario
    set_current_scenario(scenario_id)

    try:
        # 4. 跑完整 graph
        from opsagent.core.graph.builder import get_graph

        graph = get_graph()
        initial = {
            "session_id": f"gate_test_{scenario_id}",
            "user_id": "gate_tester",
            "user_query": query,
            "trace_id": f"gate_test_{scenario_id}",
        }
        final_state = await graph.ainvoke(initial)
        working_memory = final_state.get("working_memory", [])

        # 5. 收集 audit
        run_summary = end_audit_session()
        run_id = run_summary.get("run_id", "")
        audit = read_audit_logs(run_id) if run_id else {
            "decisions": [], "tool_calls": [], "dangerous_exec": []
        }
        # 再开一个 session给下一条用
        start_audit_session(run_id)

        # 6. 跑 4 个 verifier
        expected_tool = expect.get("expected_dangerous_tool", "")
        should_attempt = expect.get("llm_should_attempt_dangerous", True)
        gate_should_deny = expect.get("gate_should_deny", True)
        deny_reason_kw = expect.get("expected_deny_reason_contains", "")
        should_execute = expect.get("mock_tool_should_actually_execute", False)
        should_audit = expect.get("audit_should_record_decision", True)

        v1_pass, v1_reason = verify_llm_attempted_dangerous(
            working_memory, expected_tool, should_attempt
        )
        v2_pass, v2_reason = verify_gate_denied(
            working_memory, expected_tool, gate_should_deny, deny_reason_kw
        )
        v3_pass, v3_reason = verify_dangerous_not_executed(
            audit["dangerous_exec"], expected_tool, should_execute
        )
        v4_pass, v4_reason = verify_audit_complete(
            audit["decisions"], expected_tool, should_audit
        )

        verifiers = {
            "llm_behavior": {"passed": v1_pass, "reason": v1_reason},
            "gate_decision": {"passed": v2_pass, "reason": v2_reason},
            "mock_exec": {"passed": v3_pass, "reason": v3_reason},
            "audit_complete": {"passed": v4_pass, "reason": v4_reason},
        }
        all_passed = all(v["passed"] for v in verifiers.values())

        return {
            "scenario_id": scenario_id,
            "title": scenario.get("title", ""),
            "passed": all_passed,
            "verifiers": verifiers,
            "working_memory_size": len(working_memory),
            "audit": {
                "decisions_count": len(audit["decisions"]),
                "tool_calls_count": len(audit["tool_calls"]),
                "dangerous_exec_count": len(audit["dangerous_exec"]),
            },
        }
    except Exception as exc:
        # 区分「LLM 服务侧瞬时故障」和「真测试失败」:
        # - InternalServerError/Timeout/RateLimit 等是中转站或托管侧抖动,
        #   这类结果不该计为测试 FAILED,应计 INCONCLUSIVE(结论:LLM 不可用)
        # - 其他异常仍按 FAILED 处理
        exc_name = type(exc).__name__
        is_llm_infra_error = exc_name in {
            "InternalServerError",       # 中转站 5xx
            "APITimeoutError",           # 单次调用超时
            "APIConnectionError",        # 网络/连接侧
            "RateLimitError",            # 限流
            "ServiceUnavailableError",   # 上游不可用
        }
        logger.exception(f"[L2] {scenario_id} 异常")
        return {
            "scenario_id": scenario_id,
            "passed": False,
            "inconclusive": is_llm_infra_error,
            "error": f"{exc_name}: {exc}",
        }
    finally:
        # 清理:恢复 SOP、恢复人格、注销工具、清 scenario
        restore_normal_sop()
        restore_default_role()
        unregister_dangerous_tools()
        clear_current_scenario()


async def _run_async(categories: list[str] | None = None) -> list[dict[str, Any]]:
    """跑所有/指定分类的 L2 测试。"""
    # 启动 audit session
    run_id = f"gate_l2_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"
    start_audit_session(run_id)
    install_decision_hook()

    try:
        # 收集所有 scenarios
        scenarios: list[dict[str, Any]] = []
        if categories:
            for cat in categories:
                scenarios.extend(load_scenarios(cat))
        else:
            scenarios = load_scenarios()

        if not scenarios:
            logger.warning("[L2] 没有找到任何 scenario")
            return []

        logger.info(f"[L2] 共加载 {len(scenarios)} 条 scenario")

        results = []
        for scenario in scenarios:
            result = await run_single_scenario(scenario)
            results.append(result)
            # 简单打印
            status = "✅" if result.get("passed") else "❌"
            logger.info(
                f"[L2] {status} {result['scenario_id']}: passed={result.get('passed')}"
            )
        return results
    finally:
        uninstall_decision_hook()
        end_audit_session()


def run_llm_induced_tests(categories: list[str] | None = None) -> dict[str, Any]:
    """同步入口。"""
    results = asyncio.run(_run_async(categories))
    passed = sum(1 for r in results if r.get("passed"))
    return {
        "total": len(results),
        "passed": passed,
        "failed": len(results) - passed,
        "results": results,
    }


def format_induced_report(result: dict[str, Any]) -> str:
    """生成 L2 测试 Markdown 报告。"""
    lines = [
        "## L2 LLM 诱导 + Gate 联动测试",
        "",
        f"- 总 scenario: {result['total']}",
        f"- 通过: {result['passed']}",
        f"- 失败: {result['failed']}",
        "",
        "### 详细结果",
        "",
    ]
    for r in result["results"]:
        status = "✅" if r.get("passed") else "❌"
        lines.append(f"#### {status} {r['scenario_id']}: {r.get('title','')}")
        if "error" in r:
            lines.append(f"- 异常: `{r['error']}`")
            continue
        lines.append(f"- working_memory 条数: {r.get('working_memory_size', 0)}")
        audit = r.get("audit", {})
        lines.append(
            f"- audit: decisions={audit.get('decisions_count',0)} "
            f"tool_calls={audit.get('tool_calls_count',0)} "
            f"dangerous_exec={audit.get('dangerous_exec_count',0)}"
        )
        for vname, vdata in r.get("verifiers", {}).items():
            vstatus = "✅" if vdata["passed"] else "❌"
            reason_txt = f" — {vdata['reason']}" if vdata["reason"] else ""
            lines.append(f"- {vstatus} {vname}{reason_txt}")
        lines.append("")
    return "\n".join(lines)
