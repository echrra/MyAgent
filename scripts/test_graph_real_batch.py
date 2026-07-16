"""批量跑真实日志 case，检验 agent 在真实数据上的诊断质量。

3 个 case 分别对应 F11/F12/F13 从真实 prod TLS 抽样合成的数据：
  - data/logs/synth/fault_F11_real_requeue_storm_claude.jsonl  (800 行真实 WARN)
  - data/logs/synth/fault_F12_real_stuck_task_claude.jsonl     (800 行真实 WARN)
  - data/logs/synth/fault_F13_real_401_mass_claude.jsonl       (800 行真实 ERRO)

Query 全部按"用户视角"编写：
  - 不透露 fault_pattern 名字
  - 不给出 SOP-Fxx 提示
  - 只描述症状 + 一到两个用户猜测（考察 agent 是否能否证）

跑完后附打印"真实事实 vs agent 结论"清单，人工核对。
"""

import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from opsagent.core.graph.builder import get_graph
from opsagent.core.graph.state import AgentState


REAL_CASES = [
    {
        "id": "R1_F11_real",
        "fault": "F11_REQUEUE_STORM (真实)",
        "query": (
            "今天早上 07:59 左右 watcher 服务突然报了大量 REQUEUE_STORM 告警，"
            "看日志里 requeueCount 冲到了 24 万，阈值才 4.3 万，超了快 6 倍，"
            "任务是 motion-training-task-status，两个 task ID 反复出现"
            "（01KT88JZ136ANZQ03YE7KJ5CAF、01KTDEVGS198XXWJ74Q6WB0V37）。"
            "看着像是消息投毒，请分析根因和处置建议。"
        ),
        "真实事实": [
            "服务=edgectl-backend-watcher；时间 07:59:23-07:59:55 共 32 秒 800 条 WARN",
            "requeueCount=245040 / threshold=43200（超 5.7 倍）",
            "只有 2 个 task 反复被重投；topic=motion-training-task-status",
            "userId 分别是 5579 / 106624005222607",
        ],
        "期望关键判断": [
            "识别为 REQUEUE_STORM / poison message / 状态机推进失败",
            "建议按 SOP-F11 处理：定位 taskId → 检查消费函数 → 隔离 → 清理",
            "不误判为基础设施故障（DNS/Kafka broker/panic）",
        ],
    },
    {
        "id": "R2_F12_real",
        "fault": "F12_STUCK_TASK (真实)",
        "query": (
            "scheduler 巡检器一直在报 STUCK_TASK，说有几个 MotionTrainingProPlus 任务"
            "卡在 PreviewReady 状态很久了，ageSeconds 显示 238 万秒（差不多 27 天前的任务），"
            "而且 autoTerminate 是 true。这个到底是巡检器在正常清理历史死任务、"
            "还是当前真的有新任务在卡？我们要不要紧急处理？"
        ),
        "真实事实": [
            "服务=edgectl-backend-scheduler；跨度 13 小时（06-24 18:40 → 06-25 07:55）",
            "每轮巡检 4 个 tasks，reconcile summary: 4 suspected stuck task(s) 每轮",
            "type=MotionTrainingProPlus, status=PreviewReady",
            "ageSeconds=2389263 → 约 27.6 天前创建，autoTerminate=true 已开启",
            "estimatedRemaining=108 / threshold=708 —— 已经严重超过预期时长",
        ],
        "期望关键判断": [
            "识别为历史遗留任务清理（不是新增卡死流），因为 age 27 天 + autoTerminate 开",
            "或至少要给出'如何区分：查最新 updatedAt/新任务时序'",
            "建议排查'为什么 autoTerminate 没生效'比'紧急抢修'更合理",
        ],
    },
    {
        "id": "R3_F13_real",
        "fault": "F13_401_mass_spread (真实)",
        "query": (
            "backend-http 突然大量报错，6 分钟内 800 条 ERRO，全是 'Response check failed: 未授权 token'，"
            "都是 socket.io 请求，token 的 kid 值都是 AimOMP，涉及好几个不同用户 id。"
            "上周做过一次密钥轮换，怀疑跟这个有关，但为什么现在才爆？"
        ),
        "真实事实": [
            "服务=edgectl-backend-http；时间 07:54:13-07:59:59 共 6 分钟 800 条 ERRO",
            "全部 socket.io 请求；kid=AimOMP",
            "涉及多个不同 userId（跨用户扩散，非单用户 token 过期）",
            "backend-http 端为 200 状态但业务层 401 —— 特殊，是 socket.io 握手层判定失败",
        ],
        "期望关键判断": [
            "识别为大规模鉴权失败（不是单用户 token 过期）",
            "关联到密钥轮换 → pm-010 postmortem",
            "指出'为什么现在才爆'的可能原因：新签发 token 用了新 kid，长连接用旧 kid",
            "建议：kid ↔ 公钥映射同步、gateway/backend-http 一致性、灰度回滚",
        ],
    },
]


async def _run_one(case: dict) -> dict:
    print("=" * 78)
    print(f"▶ [{case['id']}] {case['fault']}")
    print(f"  Query: {case['query'][:120]}...")
    print("-" * 78)

    graph = get_graph()
    initial: AgentState = {
        "session_id": f"real-batch-{case['id']}-{int(time.time())}",
        "user_id": "manual-tester",
        "user_query": case["query"],
    }

    started = time.perf_counter()
    final = await graph.ainvoke(initial)
    elapsed = time.perf_counter() - started

    wm = final.get("working_memory") or []
    ans = final.get("final_answer") or ""

    print(f"\n📊 耗时 {elapsed:.1f}s / 工具调用 {len(wm)} 次")
    for i, r in enumerate(wm, 1):
        flag = "✓" if r.get("success") else "✗"
        args = json.dumps(r.get("args") or {}, ensure_ascii=False)[:110]
        print(f"   [{i}] {flag} {r.get('tool_name')}({args}) {r.get('latency_ms')}ms")

    print(f"\n💬 答案（前 700 字）:")
    print("   " + ans[:700].replace("\n", "\n   "))

    print(f"\n🔍 真实事实（对照用）:")
    for f in case["真实事实"]:
        print(f"   - {f}")
    print(f"\n🎯 期望关键判断:")
    for k in case["期望关键判断"]:
        print(f"   - {k}")
    print()

    return {"id": case["id"], "elapsed": elapsed, "tool_calls": len(wm), "answer": ans}


async def _main() -> None:
    # 支持 --only R2_F12_real,R3_F13_real 只跑子集（例如上次某 case 挂了要重跑）
    only = None
    if len(sys.argv) > 1 and sys.argv[1] == "--only" and len(sys.argv) > 2:
        only = set(sys.argv[2].split(","))
    summary = []
    for case in REAL_CASES:
        if only and case["id"] not in only:
            continue
        # 单 case 失败不影响后续 —— LLM timeout / 429 quota 也算 case 结果
        try:
            r = await _run_one(case)
        except Exception as exc:
            print(f"\n❌ [{case['id']}] 抛异常，跳过后续步骤：{type(exc).__name__}: {exc}")
            r = {"id": case["id"], "elapsed": -1, "tool_calls": 0, "answer": f"[error] {exc}"}
        summary.append(r)

    print("=" * 78)
    print("总览")
    print("=" * 78)
    for r in summary:
        print(f"  {r['id']}: {r['elapsed']:.1f}s / {r['tool_calls']} 工具调用")


if __name__ == "__main__":
    asyncio.run(_main())
