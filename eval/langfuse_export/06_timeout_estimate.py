"""06 - 超时推算：用 worker latency 反推各 run 的 search_sop 超时数。

标定尺（2026-06-29 由两次有完整日志的 chat run 标定）：
    worker latency >=150s  ≈  一次 search_sop 双超时彻底失败（60s+60s+LLM）
    验证：chat Run1 >=150s=2 ↔ 实测失败 2；chat Run2 >=150s=20 ↔ 实测失败 20（双点吻合）

用途：日志被清理、worker 内工具调用又无独立 span 时，靠 worker latency 间接估算
各 run 的 search_sop 超时数，定位线程池枯竭。详见 docs/11 §7.3-7.5。

依赖：先跑 01 + 02 生成 data/worker_byrun.json。
新 run 分析：在下方 RUNS 追加 (run_id, 标签, cite, total, 实测失败或'—')。
"""
import json
from pathlib import Path

DATA = Path(__file__).resolve().parent / "data" / "worker_byrun.json"
DIRTY = 300.0  # >300s 视为 span endTime 损坏，剔除

# (run_id, 标签, cite, total, 实测search_sop彻底失败数 或 '—'(无日志))
RUNS = [
    ("eval_20260626_111002_2a0d4a", "chat Run1(高峰)",   0.755, 0.815, "2(实测)"),
    ("eval_20260629_053233_aa1d87", "chat Run2(低峰)",   0.655, 0.783, "20(实测)"),
    ("eval_20260626_045312_21e367", "new v3base",        0.655, 0.753, "—"),
    ("eval_20260621_065301_e05cad", "最初v3base(Run1)",  0.593, 0.739, "—"),
    ("eval_20260626_030323_083f23", "bugfix(有Semaphore)", 0.664, 0.776, "—"),
    ("eval_20260622_013038_995649", "v3历史高峰(Run3)",  0.745, 0.826, "—"),
    ("eval_20260622_030712_d729b8", "v3历史低峰(Run4)",  0.598, 0.742, "—"),
]


def main():
    byrun = json.load(open(DATA))
    print("标定尺: worker >=150s ≈ 一次 search_sop 双超时彻底失败\n")
    print(f"{'run':<20}{'cite':<7}{'total':<7}{'实测失败':<9}{'wspan':<6}"
          f"{'>=120s':<8}{'>=150s(≈失败)':<14}{'P90':<7}{'均值'}")
    for rid, label, cite, total, real in RUNS:
        raw = sorted(byrun.get(rid, []))
        s = [x for x in raw if x <= DIRTY]
        n = len(s)
        if not n:
            print(f"{label:<20} 无数据")
            continue
        ge120 = sum(1 for x in s if x >= 120)
        ge150 = sum(1 for x in s if x >= 150)
        p90 = s[min(n - 1, int(n * 0.9))]
        print(f"{label:<20}{cite:<7}{total:<7}{real:<9}{n:<6}"
              f"{ge120:<8}{ge150:<14}{p90:<7.1f}{sum(s)/n:.1f}")


if __name__ == "__main__":
    main()
