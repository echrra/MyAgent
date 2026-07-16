"""一次性脚本：用真实 prod TLS 日志（task 01KWBYV0...）跑一次 agent 诊断，看效果。

背景：
- data/logs/synth/fault_F99_real_01KWBYV0_motion_tilt_claude.jsonl 已经被 search_logs 工具
  作为合成日志的一员加载（786 行里有 306 行是真实 prod 日志）
- agent 不知道这是真实数据，按"通用故障诊断"路径处理即可

期望观察：
- agent 是否会用 search_logs(service="linkcraft-backend-watcher", keyword=...) 命中
- agent 能否给出"业务校验失败 motion_tilt_danger，伴随 algo HTTP 404"这类结论
- 是否会误判为基础设施问题（DNS / Kafka / panic 等）
"""

import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from opsagent.core.graph.builder import get_graph
from opsagent.core.graph.state import MAX_ITERATIONS, AgentState


REAL_CASE = {
    "name": "real_prod_01KWBYV0_motion_tilt",
    "query": (
        "linkcraft-backend-watcher 服务今天报多个 task 处理失败，"
        "其中 task 01KWBYV0WAYVGXJHYFRRAW7KSG 出现 motion_tilt_danger 错误，"
        "watcher 似乎在反复 retry。请分析根因和处置建议。"
    ),
}


async def _run_one(case: dict) -> None:
    print("=" * 78)
    print(f"▶ {case['name']}")
    print(f"  Query: {case['query']}")
    print("-" * 78)

    graph = get_graph()
    initial: AgentState = {
        "session_id": f"real-tls-{int(time.time())}",
        "user_id": "manual-tester",
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
        flag = "✓" if rec.get("success") else "✗"
        args = rec.get("args") or rec.get("tool_args") or {}
        args_str = json.dumps(args, ensure_ascii=False)[:120]
        print(f"     [{idx}] {flag} {rec.get('tool_name')}({args_str}) ({rec.get('latency_ms')}ms)")

    print(f"\n💬 最终答案")
    print("   " + final_answer.replace("\n", "\n   "))

    if final_state.get("error"):
        print(f"\n⚠️ error 字段：{final_state['error']}")


if __name__ == "__main__":
    asyncio.run(_run_one(REAL_CASE))
