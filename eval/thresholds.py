"""回归门禁 —— 跑完评测后自动比对阈值，破线返回非零退出码，防改动偷偷退化。

为什么要这个模块：
现有流程是「跑完 → 人眼对比历史报告」。Wiki 上线后两轮跑分分别是 0.878 和
0.862 —— 人眼能发现，但如果改动一点一点侵蚀分数（0.878 → 0.875 → 0.870 →
0.865），人眼很容易漏掉。门禁把「总分/cite/超时率/P95/工具调用数」设成硬阈值，
破线 → 退出码非零 → CI 红灯，不用人肉盯。

阈值格式（baseline.json）：
{
  "total_score_min": 0.80,         // 总分均值下限
  "citation_score_min": 0.85,      // 引用命中率下限
  "timeout_rate_max": 0.05,        // 超时率上限（5%）
  "p95_latency_ms_max": 300000,    // P95 延迟上限（毫秒）
  "tool_count_mean_max": 5.0,      // 工具调用均值上限
  "forbidden_rate_max": 0.15       // forbidden 触发率上限
}

用法（CLI）：
  python -m eval.thresholds --report eval/reports/eval_XXX.md
  python -m eval.thresholds --report eval/reports/eval_XXX.md --baseline eval/baseline.json

  Exit code: 0 = 全部通过，1 = 有破线项

用法（嵌入 runner）：
  from eval.thresholds import check_thresholds, GateResult
  result = check_thresholds(summary, baseline_path)
  if not result.passed:
      print(result.violations)
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .runner import EvalRunSummary  # noqa: F401

# ====================== 数据结构 ======================


@dataclass
class GateViolation:
    """单条阈值破线记录。"""

    metric: str           # 指标名
    threshold: float      # 阈值
    actual: float         # 实际值
    direction: str        # "min" = 低于阈值破线, "max" = 高于阈值破线
    message: str = ""     # 人类可读描述


@dataclass
class GateResult:
    """门禁检查结果。"""

    passed: bool
    n_checks: int                       # 检查了多少项
    violations: list[GateViolation] = field(default_factory=list)
    metrics_snapshot: dict[str, float] = field(default_factory=dict)


# ====================== 阈值定义 ======================


# 默认阈值（未提供 baseline.json 时使用）
# 阈值来源备注：
#   - tool_count_mean 12.0：W3 时代的 6.0 是单 ReAct 预算；v3 Multi-Agent 是
#     「Coordinator 生成 3 个假设 × 每个 worker 独立跑 3-4 工具」结构，实测
#     2026-09-07 首轮 eval harness 两轮 tool_count_mean 集中在 9.98~10.07，
#     属架构真实成本而非异常。6.0 在 v3 必然常红，改用 12.0 容纳 v3 余量。
DEFAULT_THRESHOLDS: dict[str, tuple[str, float]] = {
    # 指标名: (方向, 阈值)
    # "min" = 实际值不得低于阈值, "max" = 实际值不得高于阈值
    "total_score_mean": ("min", 0.75),
    "citation_score_mean": ("min", 0.80),
    "timeout_rate": ("max", 0.10),
    "p95_latency_ms": ("max", 400_000),
    "tool_count_mean": ("max", 12.0),
    "forbidden_rate": ("max", 0.20),
}


def load_baseline(path: Path | str) -> dict[str, tuple[str, float]]:
    """从 JSON 文件加载自定义阈值。

    文件格式：
    {
      "total_score_mean": {"direction": "min", "value": 0.80},
      "timeout_rate": {"direction": "max", "value": 0.05},
      ...
    }

    也支持简写形式（自动推断方向）：
    {
      "total_score_mean": 0.80,       // 含 "_min" 或已知下限指标 → min
      "timeout_rate": 0.05,           // 含 "_max" 或已知上限指标 → max
    }
    """
    path = Path(path)
    if not path.exists():
        return DEFAULT_THRESHOLDS

    raw = json.loads(path.read_text(encoding="utf-8"))
    thresholds: dict[str, tuple[str, float]] = {}

    # 已知方向映射
    _min_metrics = {"total_score_mean", "citation_score_mean", "conclusion_score_mean",
                    "tool_score_mean", "pass_rate"}
    _max_metrics = {"timeout_rate", "p95_latency_ms", "p50_latency_ms",
                    "tool_count_mean", "forbidden_rate", "error_rate"}

    for key, val in raw.items():
        if isinstance(val, dict):
            direction = val.get("direction", "")
            value = float(val.get("value", 0))
            if not direction:
                # 自动推断
                direction = "min" if key in _min_metrics else "max"
            thresholds[key] = (direction, value)
        elif isinstance(val, (int, float)):
            # 简写：按指标名推断方向
            direction = "min" if key in _min_metrics else "max"
            thresholds[key] = (direction, float(val))

    return thresholds


# ====================== 指标计算 ======================


def compute_metrics(summary: "EvalRunSummary") -> dict[str, float]:
    """从 EvalRunSummary 计算所有门禁指标。"""
    scored = [r for r in summary.results if r.eval_result is not None]
    total = summary.total_cases

    if not scored:
        return {
            "total_score_mean": 0.0,
            "citation_score_mean": 0.0,
            "conclusion_score_mean": 0.0,
            "tool_score_mean": 0.0,
            "timeout_rate": 0.0,
            "error_rate": 0.0,
            "forbidden_rate": 0.0,
            "p50_latency_ms": 0.0,
            "p95_latency_ms": 0.0,
            "tool_count_mean": 0.0,
            "pass_rate": 0.0,
        }

    total_scores = [r.eval_result.total_score for r in scored]
    cite_scores = [r.eval_result.citation_score for r in scored]
    concl_scores = [r.eval_result.conclusion_score for r in scored]
    tool_scores = [r.eval_result.tool_score for r in scored]
    latencies = [r.latency_ms for r in scored]
    tool_counts = [float(r.tool_count) for r in scored]

    n_timeout = sum(
        1 for r in summary.results
        if r.error and "timeout" in r.error.lower()
    )
    n_forbidden = sum(
        1 for r in scored if r.eval_result.forbidden_penalty > 0
    )

    sorted_lat = sorted(latencies)
    p95_idx = int(len(sorted_lat) * 0.95)

    return {
        "total_score_mean": statistics.mean(total_scores),
        "citation_score_mean": statistics.mean(cite_scores),
        "conclusion_score_mean": statistics.mean(concl_scores),
        "tool_score_mean": statistics.mean(tool_scores),
        "timeout_rate": n_timeout / total if total else 0.0,
        "error_rate": summary.error_count / total if total else 0.0,
        "forbidden_rate": n_forbidden / total if total else 0.0,
        "p50_latency_ms": statistics.median(latencies) if latencies else 0.0,
        "p95_latency_ms": sorted_lat[min(p95_idx, len(sorted_lat) - 1)] if latencies else 0.0,
        "tool_count_mean": statistics.mean(tool_counts) if tool_counts else 0.0,
        "pass_rate": sum(1 for s in total_scores if s >= 0.7) / len(total_scores),
    }


# ====================== 门禁检查 ======================


def check_thresholds(
    summary: "EvalRunSummary",
    baseline_path: Path | str | None = None,
) -> GateResult:
    """用阈值门禁检查一轮评测结果。

    Args:
        summary: 评测运行汇总
        baseline_path: 自定义阈值 JSON 路径；None 用默认阈值

    Returns:
        GateResult（passed=True 表示全部通过）
    """
    thresholds = (
        load_baseline(baseline_path) if baseline_path else DEFAULT_THRESHOLDS
    )
    metrics = compute_metrics(summary)
    violations: list[GateViolation] = []

    for metric_name, (direction, threshold) in thresholds.items():
        actual = metrics.get(metric_name)
        if actual is None:
            continue

        if direction == "min" and actual < threshold:
            violations.append(GateViolation(
                metric=metric_name,
                threshold=threshold,
                actual=actual,
                direction="min",
                message=f"{metric_name} = {actual:.4f} < 下限 {threshold:.4f}",
            ))
        elif direction == "max" and actual > threshold:
            violations.append(GateViolation(
                metric=metric_name,
                threshold=threshold,
                actual=actual,
                direction="max",
                message=f"{metric_name} = {actual:.4f} > 上限 {threshold:.4f}",
            ))

    return GateResult(
        passed=len(violations) == 0,
        n_checks=len(thresholds),
        violations=violations,
        metrics_snapshot=metrics,
    )


def format_gate_report(result: GateResult, run_id: str = "") -> str:
    """生成门禁检查报告（Markdown 文本块，可追加到评测报告尾部）。"""
    lines = [
        "",
        "## 回归门禁",
        "",
    ]

    if result.passed:
        lines.append(f"✅ **全部通过**（{result.n_checks} 项检查）")
    else:
        lines.append(f"❌ **{len(result.violations)}/{result.n_checks} 项破线**")

    if result.violations:
        lines.append("")
        lines.append("| 指标 | 阈值 | 实际 | 方向 |")
        lines.append("|---|---|---|---|")
        for v in result.violations:
            arrow = "≥" if v.direction == "min" else "≤"
            lines.append(
                f"| {v.metric} | {arrow} {v.threshold:.4f} | {v.actual:.4f} | {v.direction} |"
            )

    lines.append("")
    lines.append("<details><summary>完整指标快照</summary>")
    lines.append("")
    lines.append("```json")
    lines.append(json.dumps(
        {k: round(v, 4) for k, v in sorted(result.metrics_snapshot.items())},
        indent=2,
    ))
    lines.append("```")
    lines.append("</details>")
    lines.append("")

    return "\n".join(lines)


# ====================== CLI ======================


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="回归门禁：检查评测报告是否破线")
    parser.add_argument("--report", type=str, required=True, help="评测报告路径 (.md)")
    parser.add_argument("--baseline", type=str, default=None, help="自定义阈值 JSON")
    parser.add_argument(
        "--quiet", action="store_true", help="只输出结果，不打印明细"
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    report_path = Path(args.report)

    if not report_path.exists():
        print(f"❌ 报告不存在: {report_path}")
        sys.exit(1)

    # 从 runner 的 Markdown 报告中提取指标（简化版：直接从文本解析）
    text = report_path.read_text(encoding="utf-8")
    metrics = _parse_report_metrics(text)

    if not metrics:
        print("❌ 无法从报告中提取指标")
        sys.exit(1)

    # 构造一个最小 summary 用于 check_thresholds
    # （CLI 模式下我们没有完整的 EvalRunSummary，直接从报告的指标对比）
    thresholds = (
        load_baseline(args.baseline) if args.baseline else DEFAULT_THRESHOLDS
    )
    violations: list[GateViolation] = []

    for metric_name, (direction, threshold) in thresholds.items():
        actual = metrics.get(metric_name)
        if actual is None:
            continue
        if direction == "min" and actual < threshold:
            violations.append(GateViolation(
                metric=metric_name, threshold=threshold, actual=actual,
                direction="min",
                message=f"{metric_name} = {actual:.4f} < 下限 {threshold:.4f}",
            ))
        elif direction == "max" and actual > threshold:
            violations.append(GateViolation(
                metric=metric_name, threshold=threshold, actual=actual,
                direction="max",
                message=f"{metric_name} = {actual:.4f} > 上限 {threshold:.4f}",
            ))

    passed = len(violations) == 0

    if not args.quiet:
        status = "✅ 全部通过" if passed else "❌ 破线"
        print(f"{status}（{len(thresholds)} 项检查，{len(violations)} 项破线）")
        for v in violations:
            print(f"  ❌ {v.message}")
        if not violations:
            for k, v in sorted(metrics.items()):
                print(f"  ✅ {k} = {v:.4f}")

    sys.exit(0 if passed else 1)


def _parse_report_metrics(text: str) -> dict[str, float]:
    """从 Markdown 报告文本中提取门禁指标（CLI 模式用）。"""
    metrics: dict[str, float] = {}

    for line in text.split("\n"):
        line = line.strip()
        if not line.startswith("|") or "---" in line:
            continue
        cells = [c.strip() for c in line.split("|") if c.strip()]
        if len(cells) < 2:
            continue

        # 总览表
        metric_map = {
            "总分均值": "total_score_mean",
            "工具 F1 均值": "tool_score_mean",
            "引用命中率": "citation_score_mean",
            "结论关键词": "conclusion_score_mean",
        }
        if cells[0] in metric_map:
            try:
                metrics[metric_map[cells[0]]] = float(cells[1])
            except (ValueError, IndexError):
                pass

        # Forbidden 触发率（百分比）
        if cells[0] == "Forbidden 触发率":
            try:
                metrics["forbidden_rate"] = float(cells[1].rstrip("%")) / 100.0
            except (ValueError, IndexError):
                pass

    return metrics


if __name__ == "__main__":
    main()
