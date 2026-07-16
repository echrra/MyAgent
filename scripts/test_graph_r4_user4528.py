"""R4 单 case：用户自报故障

用户 4528（username: 15927293009）在 2026-06-17 13:49:53 报了个问题，
TraceId 是 377f928b54c8b918d3bf746dcc86a4c1，用户原话："这是啥情况？！"

这个 case 的特殊性：
- 完全用户视角，agent 得先"翻译"用户吐槽 → 排查动作
- 只给了 4 个线索（userId / phone / time / trace），要 agent 自己拼查询
- 时间在 06-17，本地日志窗口 06-24~06-30 —— agent 是否能识别"时间窗外"是加分项
"""

import asyncio
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from opsagent.core.graph.builder import get_graph
from opsagent.core.graph.state import AgentState

# 真实 case runner：显式开启 TLS fallback。普通合成 eval 不会自动打云端。
os.environ.setdefault("AGENT_ENABLE_TLS_FALLBACK", "1")


CASE = {
    "id": "R4_user4528",
    "fault": "用户自报（无明确故障模式）",
    "query": (
        "用户 4528（username: 15927293009）在北京时间 2026-06-17 13:49:53 反馈说'这是啥情况？！'，"
        "TraceId 是 377f928b54c8b918d3bf746dcc86a4c1。"
        "这是一个真实生产 case，请通过 TraceId 查真实日志，帮我确认这条请求到底怎么了："
        "哪个服务出问题、用户看到什么错误、要不要处理。"
    ),
    "真实事实": [
        "线索：userId=4528 / phone=15927293009 / time=2026-06-17T13:49:53 / trace=377f928b54c8b918d3bf746dcc86a4c1",
        "本地日志窗口是 06-24~06-30，2026-06-17 大概率在本地采样之外",
        "agent 应识别'时间窗外'并给出明确回复，而不是编造结论",
    ],
    "期望关键判断": [
        "能识别数据不足/时间窗外，而不是硬凑证据",
        "如果查到，说明具体服务 + 错误码 + 建议",
        "如果查不到，明确告知'现有日志内未命中该 TraceId'并建议怎么补",
    ],
}


async def main() -> None:
    print("=" * 78)
    print(f"▶ [{CASE['id']}] {CASE['fault']}")
    print(f"  Query: {CASE['query']}")
    print("-" * 78)

    graph = get_graph()
    initial: AgentState = {
        "session_id": f"real-{CASE['id']}-{int(time.time())}",
        "user_id": "manual-tester",
        "user_query": CASE["query"],
    }

    started = time.perf_counter()
    final = await graph.ainvoke(initial)
    elapsed = time.perf_counter() - started

    wm = final.get("working_memory") or []
    ans = final.get("final_answer") or ""

    print(f"\n📊 耗时 {elapsed:.1f}s / 工具调用 {len(wm)} 次")
    for i, r in enumerate(wm, 1):
        flag = "✓" if r.get("success") else "✗"
        args = json.dumps(r.get("args") or {}, ensure_ascii=False)[:140]
        print(f"   [{i}] {flag} {r.get('tool_name')}({args}) {r.get('latency_ms')}ms")

    print(f"\n💬 答案（完整）:\n{ans}")

    print(f"\n🔍 真实事实（对照用）:")
    for f in CASE["真实事实"]:
        print(f"   - {f}")
    print(f"\n🎯 期望关键判断:")
    for k in CASE["期望关键判断"]:
        print(f"   - {k}")


if __name__ == "__main__":
    asyncio.run(main())
