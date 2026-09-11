"""独立入口:跑 gate_harness_test 的一层或多层测试。

用法:
    # L1 矩阵测试(秒级,可入 CI)
    python scripts/run_gate_tests.py --layer L1

    # L3 强制 tool_call 注入(秒级)
    python scripts/run_gate_tests.py --layer L3

    # L2 LLM 诱导(分钟级,需要 LLM API)
    python scripts/run_gate_tests.py --layer L2

    # L1+L3(合并,适合 CI)
    python scripts/run_gate_tests.py --layer L1,L3

    # 全部
    python scripts/run_gate_tests.py --layer all

    # 只跑某个 category 的 L2
    python scripts/run_gate_tests.py --layer L2 --category direct_invoke
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from gate_harness_test.runners import (  # noqa: E402
    format_forced_report,
    format_induced_report,
    format_matrix_report,
    run_forced_call_tests,
    run_llm_induced_tests,
    run_matrix_tests,
)

REPORTS_ROOT = PROJECT_ROOT / "gate_harness_test" / "reports"


def save_report(content: str, run_id: str, layer: str) -> Path:
    """把报告落地到 gate_harness_test/reports/{run_id}_{layer}.md"""
    REPORTS_ROOT.mkdir(parents=True, exist_ok=True)
    path = REPORTS_ROOT / f"{run_id}_{layer}.md"
    path.write_text(content, encoding="utf-8")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="Gate 测试独立入口")
    parser.add_argument(
        "--layer",
        default="L1,L3",
        help="要跑哪些层(L1/L2/L3),逗号分隔;'all'=全跑",
    )
    parser.add_argument(
        "--category",
        default=None,
        help="L2 专用:只跑某个 category 目录(direct_invoke/role_override/...)",
    )
    args = parser.parse_args()

    layers = [s.strip().upper() for s in args.layer.split(",") if s.strip()]
    if "ALL" in layers:
        layers = ["L1", "L2", "L3"]
    categories = [args.category] if args.category else None

    run_id = f"gate_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"
    all_pass = True
    print(f"🚦 Gate Harness Test run_id={run_id}")
    print(f"   layers={layers} categories={categories or 'all'}")
    print("=" * 60)

    if "L1" in layers:
        print("\n[1/3] L1 矩阵测试")
        l1 = run_matrix_tests()
        print(format_matrix_report(l1))
        save_report(format_matrix_report(l1), run_id, "L1")
        if l1["failed"] > 0:
            all_pass = False

    if "L3" in layers:
        print("\n[2/3] L3 强制 tool_call 注入测试")
        l3 = run_forced_call_tests()
        print(format_forced_report(l3))
        save_report(format_forced_report(l3), run_id, "L3")
        if l3["failed"] > 0:
            all_pass = False

    if "L2" in layers:
        print("\n[3/3] L2 LLM 诱导 + Gate 联动测试(慢,需要 LLM API)")
        l2 = run_llm_induced_tests(categories)
        print(format_induced_report(l2))
        save_report(format_induced_report(l2), run_id, "L2")
        if l2["failed"] > 0:
            all_pass = False

    print("\n" + "=" * 60)
    if all_pass:
        print("🎉 所有层测试通过 — gate 防御有效")
        return 0
    else:
        print("🔴 有 gate 失守 — 立即检查 policy")
        return 1


if __name__ == "__main__":
    sys.exit(main())
