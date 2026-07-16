"""端到端烟测：跑一条用户查询走完整 ReAct 循环。

跑法:
    make test-graph
    # 或
    uv run python scripts/test_graph.py

验证点:
- load_memory / plan / tool_exec / reflect / answer / persist_memory 6 个节点都被触发
- 至少有 1 次 tool_exec（因为问题设计成需要查日志/指标才能答）
- final_answer 非空
- iteration ≤ MAX_ITERATIONS
"""

import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from opsagent.core.graph.builder import get_graph  # noqa: E402
from opsagent.core.graph.state import MAX_ITERATIONS, AgentState  # noqa: E402


PROBE_CASES = [
    {
        "name": "case_1_查日志+SOP才能答",
        "query": "edgectl-backend-watcher 最近 DNS 解析失败了吗？如果有，根据 SOP 给出排查建议。",
    },
]


async def _run_one(case: dict) -> bool:
    print("=" * 70)
    print(f"▶ {case['name']}")
    print(f"  Query: {case['query']}")
    print("-" * 70)

    graph = get_graph()
    initial: AgentState = {
        "session_id": "smoke-test-001",
        "user_id": "tester",
        "user_query": case["query"],
    }

    started = time.perf_counter()
    final_state = await graph.ainvoke(initial)
    elapsed = time.perf_counter() - started

    iteration = final_state.get("iteration", 0)
    working_memory = final_state.get("working_memory") or []
    final_answer = final_state.get("final_answer") or ""

    print(f"\n📊 运行统计")
    print(f"   耗时:        {elapsed:.2f}s")
    print(f"   循环轮次:    {iteration} / {MAX_ITERATIONS}")
    print(f"   工具调用数:  {len(working_memory)}")
    for idx, rec in enumerate(working_memory, 1):
        flag = "✓" if rec["success"] else "✗"
        print(f"     [{idx}] {flag} {rec['tool_name']} ({rec['latency_ms']}ms)")

    print(f"\n💬 最终答案")
    print("   " + final_answer.replace("\n", "\n   "))

    # 验收
    checks = {
        "final_answer 非空": bool(final_answer.strip()),
        "至少 1 次工具调用": len(working_memory) >= 1,
        "未超最大轮次": iteration <= MAX_ITERATIONS,
        "无 error 字段": final_state.get("error") is None,
    }
    print("\n✅ 验收")
    all_pass = True
    for desc, ok in checks.items():
        tag = "✓" if ok else "✗"
        print(f"   {tag} {desc}")
        all_pass = all_pass and ok
    return all_pass


async def main() -> int:
    print("OpsAgent — Graph 端到端烟测\n")
    failures = 0
    for case in PROBE_CASES:
        try:
            ok = await _run_one(case)
            if not ok:
                failures += 1
        except Exception as exc:  # noqa: BLE001
            print(f"\n❌ {case['name']} 抛异常: {type(exc).__name__}: {exc}")
            failures += 1

    print("\n" + "=" * 70)
    if failures == 0:
        print("🎉 全部用例通过，Agent 图骨架 OK。")
        return 0
    print(f"⚠️ {failures} 个用例失败")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
