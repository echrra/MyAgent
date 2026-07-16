"""W5-B 评测编排器 —— 加载 case → 调 Agent → 打分 → Markdown 报告。

使用方式:
  python -m eval.runner               # 全量 55 条
  python -m eval.runner --quick       # quick subset 10 条
  python -m eval.runner --ids E001,E005
  python -m eval.runner --concurrency 2 --timeout 210
  python -m eval.runner --runs 3      # 跑 3 次取中位数（消除 LLM 非确定性）
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from loguru import logger

from eval.dataset import CASES_DIR, load_cases, load_quick_subset
from eval.metrics.scorer import EvalResult, score_case

# ====================== 数据结构 ======================


@dataclass
class CaseRunResult:
    """单条 case 的运行结果（含打分 + 工程指标）。"""

    case_id: str
    fault_pattern: str
    difficulty: str
    eval_result: EvalResult | None = None
    latency_ms: float = 0.0
    tool_count: int = 0
    error: str | None = None


@dataclass
class EvalRunSummary:
    """整批运行汇总。"""

    run_id: str
    timestamp: str
    total_cases: int
    success_count: int
    error_count: int
    results: list[CaseRunResult] = field(default_factory=list)


# ====================== Runner ======================


class EvalRunner:
    """评测编排器：并发调 Agent、打分、生成报告。"""

    def __init__(
        self,
        cases: list[dict[str, Any]],
        concurrency: int = 3,
        timeout: float = 120.0,
    ):
        self.cases = cases
        self.concurrency = concurrency
        self.timeout = timeout
        self.run_id = f"eval_{datetime.now(UTC).strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"

    async def run(self) -> EvalRunSummary:
        """跑完所有 case，返回汇总。"""
        sem = asyncio.Semaphore(self.concurrency)
        tasks = [self._run_single(case, sem) for case in self.cases]
        results = await asyncio.gather(*tasks)

        success_count = sum(1 for r in results if r.error is None)
        return EvalRunSummary(
            run_id=self.run_id,
            timestamp=datetime.now(UTC).isoformat(),
            total_cases=len(results),
            success_count=success_count,
            error_count=len(results) - success_count,
            results=results,
        )

    async def _run_single(
        self, case: dict[str, Any], sem: asyncio.Semaphore
    ) -> CaseRunResult:
        """单条 case：获取信号量 → 调用 Agent → 打分。超时/网络错误自动重试 1 次。"""
        case_id = case.get("id", "?")
        fault_pattern = case.get("fault_pattern", "?")
        difficulty = case.get("difficulty", "?")
        max_attempts = 2  # 原始 + 1 次重试

        for attempt in range(max_attempts):
            async with sem:
                try:
                    result = await asyncio.wait_for(
                        self._invoke_and_score(case),
                        timeout=self.timeout,
                    )
                    # 成功拿到结果（包括 Agent 正常返回低分的情况）
                    return result
                except TimeoutError:
                    if attempt < max_attempts - 1:
                        logger.warning(
                            f"[eval] {case_id} 超时 {self.timeout}s（第 {attempt+1} 次），重试..."
                        )
                        continue
                    return CaseRunResult(
                        case_id=case_id,
                        fault_pattern=fault_pattern,
                        difficulty=difficulty,
                        error=f"timeout ({self.timeout}s)",
                    )
                except Exception as e:
                    # 网络/连接类错误重试，其他错误直接返回
                    err_name = type(e).__name__
                    is_retryable = any(k in err_name for k in (
                        "Timeout", "Connect", "Connection", "Network", "httpx",
                    )) or "timeout" in str(e).lower()
                    if is_retryable and attempt < max_attempts - 1:
                        logger.warning(
                            f"[eval] {case_id} 网络异常 {err_name}（第 {attempt+1} 次），重试..."
                        )
                        continue
                    return CaseRunResult(
                        case_id=case_id,
                        fault_pattern=fault_pattern,
                        difficulty=difficulty,
                        error=repr(e),
                    )
        # 不应该到这里，兜底返回
        return CaseRunResult(
            case_id=case_id,
            fault_pattern=fault_pattern,
            difficulty=difficulty,
            error="max_attempts_exhausted",
        )

    async def _invoke_and_score(self, case: dict[str, Any]) -> CaseRunResult:
        """调用 Agent graph，提取结果，打分。支持单轮和多轮 case。"""
        from opsagent.core.graph.builder import get_graph
        from opsagent.core.observability import (
            create_trace,
            end_trace,
            flush,
            score_trace,
            span_context,
        )

        case_id = case.get("id", "?")
        role = case.get("user_profile", {}).get("role", "SRE")
        is_multi_turn = case.get("type") == "multi_turn"

        graph = get_graph()
        session_id = f"{self.run_id}_{case_id}"
        trace_id = f"{self.run_id}_{case_id}"

        # 多轮 case：依次调用每轮 query，同一 session_id 保持记忆连续
        if is_multi_turn:
            turns = case.get("turns", [])
            if not turns:
                turns = [{"query": case.get("query", "")}]
        else:
            turns = [{"query": case["query"]}]

        # 先创建 trace 对象（注册到缓存），再开 span（span 会关联到这个 trace）
        create_trace(
            trace_id=trace_id,
            session_id=session_id,
            user_id=f"eval_{role}",
            name=f"eval_{case_id}",
            metadata={"fault_pattern": case.get("fault_pattern"), "difficulty": case.get("difficulty")},
        )

        with span_context(trace_id, f"eval_{case_id}", {"query": turns[-1].get("query", "")}):

            started = time.perf_counter()

            # 逐轮调用，前置轮只建立上下文，不打分
            final_state: dict[str, Any] = {}
            for _turn_idx, turn in enumerate(turns):
                query = turn if isinstance(turn, str) else turn.get("query", "")
                initial: dict[str, Any] = {
                    "session_id": session_id,
                    "user_id": f"eval_{role}",
                    "user_query": query,
                    "trace_id": trace_id,
                }
                final_state = await graph.ainvoke(initial)

            elapsed_ms = (time.perf_counter() - started) * 1000

            # 只对最后一轮的结果打分
            working_memory = final_state.get("working_memory") or []
            final_answer = final_state.get("final_answer") or ""

            eval_result = score_case(case, working_memory, final_answer)

            # 在 span context 内打分
            score_trace(trace_id, "l4_total", eval_result.total_score)

        end_trace(trace_id)
        flush()

        return CaseRunResult(
            case_id=case_id,
            fault_pattern=case.get("fault_pattern", "?"),
            difficulty=case.get("difficulty", "?"),
            eval_result=eval_result,
            latency_ms=round(elapsed_ms, 1),
            tool_count=len(working_memory),
        )


# ====================== 报告生成 ======================


def generate_report(summary: EvalRunSummary, output_path: Path) -> None:
    """生成 Markdown 评测报告。"""
    results = summary.results
    scored = [r for r in results if r.eval_result is not None]

    # 总览指标
    if scored:
        total_scores = [r.eval_result.total_score for r in scored]
        tool_scores = [r.eval_result.tool_score for r in scored]
        cite_scores = [r.eval_result.citation_score for r in scored]
        concl_scores = [r.eval_result.conclusion_score for r in scored]
        forbidden_rate = sum(
            1 for r in scored if r.eval_result.forbidden_penalty > 0
        ) / len(scored)
        latencies = [r.latency_ms for r in scored]
        tool_counts = [r.tool_count for r in scored]
    else:
        total_scores = tool_scores = cite_scores = concl_scores = [0.0]
        forbidden_rate = 0.0
        latencies = [0.0]
        tool_counts = [0]

    def _p50(data: list[float]) -> float:
        return statistics.median(data) if data else 0.0

    def _p95(data: list[float]) -> float:
        if not data:
            return 0.0
        sorted_d = sorted(data)
        idx = int(len(sorted_d) * 0.95)
        return sorted_d[min(idx, len(sorted_d) - 1)]

    def _mean(data: list[float]) -> float:
        return statistics.mean(data) if data else 0.0

    lines = [
        f"# Eval Report: {summary.run_id}",
        "",
        f"**时间**: {summary.timestamp}  ",
        f"**总条数**: {summary.total_cases} | **成功**: {summary.success_count} | **错误**: {summary.error_count}",
        "",
        "## 总览",
        "",
        "| 指标 | 数值 |",
        "|---|---|",
        f"| 总分均值 | {_mean(total_scores):.3f} |",
        f"| 工具 F1 均值 | {_mean(tool_scores):.3f} |",
        f"| 引用命中率 | {_mean(cite_scores):.3f} |",
        f"| 结论关键词 | {_mean(concl_scores):.3f} |",
        f"| Forbidden 触发率 | {forbidden_rate:.1%} |",
        "",
        "## 工程指标",
        "",
        "| 维度 | P50 | P95 | 均值 |",
        "|---|---|---|---|",
        f"| 端到端延迟(ms) | {_p50(latencies):.0f} | {_p95(latencies):.0f} | {_mean(latencies):.0f} |",
        f"| 工具调用次数 | {_p50([float(x) for x in tool_counts]):.1f} | {_p95([float(x) for x in tool_counts]):.1f} | {_mean([float(x) for x in tool_counts]):.1f} |",
        "",
    ]

    # 按故障类型
    lines.append("## 按故障类型")
    lines.append("")
    lines.append("| 类型 | Case数 | 均分 | 最低分case |")
    lines.append("|---|---|---|---|")

    pattern_groups: dict[str, list[CaseRunResult]] = {}
    for r in scored:
        pattern_groups.setdefault(r.fault_pattern, []).append(r)
    for pattern in sorted(pattern_groups.keys()):
        group = pattern_groups[pattern]
        avg = _mean([r.eval_result.total_score for r in group])
        worst = min(group, key=lambda r: r.eval_result.total_score)
        lines.append(
            f"| {pattern} | {len(group)} | {avg:.3f} | {worst.case_id}({worst.eval_result.total_score:.2f}) |"
        )
    lines.append("")

    # 按难度
    lines.append("## 按难度")
    lines.append("")
    lines.append("| 难度 | Case数 | 均分 |")
    lines.append("|---|---|---|")
    for diff in ("easy", "medium", "hard"):
        group = [r for r in scored if r.difficulty == diff]
        if group:
            avg = _mean([r.eval_result.total_score for r in group])
            lines.append(f"| {diff} | {len(group)} | {avg:.3f} |")
    lines.append("")

    # 失败 case (score < 0.7)
    failed = [r for r in scored if r.eval_result.total_score < 0.7]
    lines.append(f"## 失败 case (score < 0.7) — {len(failed)} 条")
    lines.append("")
    if failed:
        lines.append("| ID | 类型 | 难度 | 总分 | 失分原因 |")
        lines.append("|---|---|---|---|---|")
        for r in sorted(failed, key=lambda x: x.eval_result.total_score):
            reasons = []
            if r.eval_result.tool_score < 0.5:
                reasons.append("工具不匹配")
            if r.eval_result.citation_score < 0.5:
                reasons.append("引用缺失")
            if r.eval_result.conclusion_score < 0.5:
                reasons.append("关键词不足")
            if r.eval_result.forbidden_penalty > 0:
                reasons.append("触发禁忌词")
            lines.append(
                f"| {r.case_id} | {r.fault_pattern} | {r.difficulty} "
                f"| {r.eval_result.total_score:.3f} | {', '.join(reasons) or '-'} |"
            )
    else:
        lines.append("无。")
    lines.append("")

    # 错误 case
    errors = [r for r in results if r.error is not None]
    if errors:
        lines.append(f"## 运行错误 — {len(errors)} 条")
        lines.append("")
        lines.append("| ID | 错误 |")
        lines.append("|---|---|")
        for r in errors:
            lines.append(f"| {r.case_id} | {r.error} |")
        lines.append("")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(lines), encoding="utf-8")


def generate_median_report(summaries: list[EvalRunSummary], output_path: Path) -> None:
    """多轮评测中位数汇总报告——消除 LLM 非确定性。"""
    n_runs = len(summaries)

    # 收集每条 case 所有轮次的分数和元信息
    case_meta: dict[str, dict[str, str]] = {}
    case_scores: dict[str, list[float]] = {}
    case_latencies: dict[str, list[float]] = {}
    error_counts: dict[str, int] = {}

    for s in summaries:
        for r in s.results:
            cid = r.case_id
            case_meta.setdefault(cid, {"fault_pattern": r.fault_pattern, "difficulty": r.difficulty})
            if r.error:
                error_counts[cid] = error_counts.get(cid, 0) + 1
            elif r.eval_result:
                case_scores.setdefault(cid, []).append(r.eval_result.total_score)
                case_latencies.setdefault(cid, []).append(r.latency_ms)

    def _median(data: list[float]) -> float:
        return statistics.median(data) if data else 0.0

    def _stdev(data: list[float]) -> float:
        return statistics.stdev(data) if len(data) >= 2 else 0.0

    def _mean(data: list[float]) -> float:
        return statistics.mean(data) if data else 0.0

    # 所有 case 的中位数分数
    all_medians = {cid: _median(scores) for cid, scores in case_scores.items()}
    all_ranges = {cid: max(scores) - min(scores) for cid, scores in case_scores.items() if scores}

    lines = [
        f"# Median Report ({n_runs} runs)",
        "",
        f"**轮次**: {n_runs} | **Case 数**: {len(case_meta)}  ",
        f"**Run IDs**: {', '.join(s.run_id for s in summaries)}",
        "",
        "## 总览（中位数）",
        "",
        "| 指标 | 数值 |",
        "|---|---|",
        f"| 中位数总分均值 | {_mean(list(all_medians.values())):.3f} |",
    ]

    # 各轮单次均值对比
    lines.append("")
    lines.append("## 各轮单次均值")
    lines.append("")
    lines.append("| Run | 总分均值 | 成功 | 错误 |")
    lines.append("|---|---|---|---|")
    for i, s in enumerate(summaries):
        scored = [r for r in s.results if r.eval_result]
        avg = _mean([r.eval_result.total_score for r in scored]) if scored else 0.0
        lines.append(f"| Run {i + 1} | {avg:.3f} | {s.success_count} | {s.error_count} |")
    lines.append("")

    # 按难度汇总中位数
    lines.append("## 按难度（中位数）")
    lines.append("")
    lines.append("| 难度 | Case数 | 中位数均分 | 通过率(≥0.7) |")
    lines.append("|---|---|---|---|")
    for diff in ("easy", "medium", "hard"):
        group = {cid: m for cid, m in all_medians.items() if case_meta.get(cid, {}).get("difficulty") == diff}
        if group:
            avg = _mean(list(group.values()))
            pass_rate = sum(1 for v in group.values() if v >= 0.7) / len(group)
            lines.append(f"| {diff} | {len(group)} | {avg:.3f} | {pass_rate:.0%} |")
    lines.append("")

    # 逐 case 对比表
    lines.append("## 逐 Case 对比")
    lines.append("")
    header = "| Case | 难度 | " + " | ".join(f"R{i+1}" for i in range(n_runs)) + " | 中位数 | 极差 | 波动 |"
    lines.append(header)
    lines.append("|---|---|" + "|---|" * n_runs + "---|---|---|")

    for cid in sorted(case_meta.keys()):
        meta = case_meta[cid]
        median = all_medians.get(cid, 0.0)
        rng = all_ranges.get(cid, 0.0)
        errs = error_counts.get(cid, 0)

        # 波动分类
        if errs == n_runs:
            volatility = "固定错误"
        elif rng == 0:
            volatility = "稳定" if median >= 0.7 else "稳定低"
        elif rng <= 0.2:
            volatility = "小"
        elif rng <= 0.3:
            volatility = "中"
        else:
            volatility = "**巨幅**"

        # 构造每轮分数列
        score_by_run: list[str] = []
        for s in summaries:
            matched = [r for r in s.results if r.case_id == cid]
            if matched and matched[0].error:
                score_by_run.append("ERR")
            elif matched and matched[0].eval_result:
                score_by_run.append(f"{matched[0].eval_result.total_score:.3f}")
            else:
                score_by_run.append("-")

        cols = " | ".join(score_by_run)
        lines.append(f"| {cid} | {meta['difficulty']} | {cols} | {median:.3f} | {rng:.3f} | {volatility} |")
    lines.append("")

    # 波动分类统计
    lines.append("## 波动统计")
    lines.append("")
    lines.append("| 类别 | Case 数 | 占比 |")
    lines.append("|---|---|---|")
    vol_counts: dict[str, int] = {}
    for cid in case_meta:
        errs = error_counts.get(cid, 0)
        rng = all_ranges.get(cid, 0.0)
        median = all_medians.get(cid, 0.0)
        if errs == n_runs:
            cat = "固定错误"
        elif rng == 0:
            cat = "完全稳定" if median >= 0.7 else "稳定低分"
        elif rng <= 0.2:
            cat = "小波动"
        elif rng <= 0.3:
            cat = "中波动"
        else:
            cat = "大波动"
        vol_counts[cat] = vol_counts.get(cat, 0) + 1
    total_cases = len(case_meta)
    for cat in ["完全稳定", "小波动", "中波动", "大波动", "稳定低分", "固定错误"]:
        n = vol_counts.get(cat, 0)
        if n > 0:
            lines.append(f"| {cat} | {n} | {n/total_cases:.0%} |")
    lines.append("")

    # 稳定低分 + 大波动列表（重点关注）
    lines.append("## 重点关注 case")
    lines.append("")
    lines.append("### 稳定低分（中位数 < 0.7 且极差 < 0.2）")
    lines.append("")
    stable_low = [(cid, all_medians[cid]) for cid in all_medians if all_medians[cid] < 0.7 and all_ranges.get(cid, 0) < 0.2]
    if stable_low:
        lines.append("| Case | 难度 | 中位数 | 类型 |")
        lines.append("|---|---|---|---|")
        for cid, med in sorted(stable_low, key=lambda x: x[1]):
            meta = case_meta[cid]
            lines.append(f"| {cid} | {meta['difficulty']} | {med:.3f} | {meta['fault_pattern']} |")
    else:
        lines.append("无。")
    lines.append("")

    lines.append("### 大波动（极差 ≥ 0.3）")
    lines.append("")
    high_vol = [(cid, all_ranges[cid]) for cid in all_ranges if all_ranges[cid] >= 0.3]
    if high_vol:
        lines.append("| Case | 难度 | 极差 | 类型 |")
        lines.append("|---|---|---|---|")
        for cid, rng in sorted(high_vol, key=lambda x: -x[1]):
            meta = case_meta[cid]
            lines.append(f"| {cid} | {meta['difficulty']} | {rng:.3f} | {meta['fault_pattern']} |")
    else:
        lines.append("无。")
    lines.append("")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(lines), encoding="utf-8")


# ====================== CLI ======================


def parse_args() -> argparse.Namespace:
    """解析命令行参数。"""
    parser = argparse.ArgumentParser(description="OpsAgent L4 端到端评测")
    parser.add_argument(
        "--dataset",
        type=str,
        default=str(CASES_DIR),
        help="评测集目录路径",
    )
    parser.add_argument("--quick", action="store_true", help="只跑 quick subset 10 条")
    parser.add_argument("--ids", type=str, default=None, help="指定 case ID（逗号分隔）")
    parser.add_argument(
        "--difficulty", type=str, default=None, help="按难度过滤（easy/medium/hard）"
    )
    parser.add_argument("--concurrency", type=int, default=3, help="并发数")
    parser.add_argument("--timeout", type=float, default=120.0, help="单 case 超时(秒)")
    parser.add_argument(
        "--output",
        type=str,
        default=None,
        help="报告输出路径（默认 eval/reports/eval_{run_id}.md）",
    )
    parser.add_argument("--ragas", action="store_true", help="启用 Ragas 指标（faithfulness + context_recall）")
    parser.add_argument("--multi-turn", action="store_true", help="只跑多轮对话 case（type=multi_turn）")
    parser.add_argument("--runs", type=int, default=1, help="评测轮数（>1 时自动输出中位数报告）")
    return parser.parse_args()


async def async_main() -> None:
    """异步主入口。"""
    args = parse_args()

    # 加载评测集
    if getattr(args, "multi_turn", False):
        cases = load_cases(args.dataset, tags=["multi_turn"])
        print(f"📦 加载多轮对话 case: {len(cases)} 条")
    elif args.quick:
        cases = load_quick_subset(args.dataset)
        print(f"📦 加载 quick subset: {len(cases)} 条")
    else:
        ids = args.ids.split(",") if args.ids else None
        cases = load_cases(args.dataset, difficulty=args.difficulty, ids=ids)
        print(f"📦 加载评测集: {len(cases)} 条")

    if not cases:
        print("⚠️  无评测 case，退出。")
        return

    n_runs = max(1, args.runs)
    all_summaries: list[EvalRunSummary] = []

    for run_idx in range(n_runs):
        runner = EvalRunner(cases, concurrency=args.concurrency, timeout=args.timeout)
        label = f"[{run_idx + 1}/{n_runs}] " if n_runs > 1 else ""
        print(
            f"🚀 {label}开始评测 run_id={runner.run_id} "
            f"(并发={args.concurrency}, 超时={args.timeout}s)"
        )
        print("-" * 60)

        summary = await runner.run()
        all_summaries.append(summary)

        # 逐条打印
        for r in summary.results:
            if r.error:
                print(f"  ✗ {r.case_id}: ERROR - {r.error}")
            else:
                score = r.eval_result.total_score if r.eval_result else 0.0
                print(f"  {'✓' if score >= 0.7 else '△'} {r.case_id}: {score:.3f} ({r.latency_ms:.0f}ms)")

        # 单轮报告
        report_path = Path(args.output) if args.output else Path("eval/reports") / f"{runner.run_id}.md"
        generate_report(summary, report_path)
        scored = [r for r in summary.results if r.eval_result]
        if scored:
            avg = statistics.mean([r.eval_result.total_score for r in scored])
            print(f"   {label}总分均值: {avg:.3f} | 成功/错误: {summary.success_count}/{summary.error_count}")
            print(f"   报告: {report_path}")
        print()

    # 多轮汇总：输出中位数报告
    if n_runs > 1:
        median_path = Path("eval/reports") / f"median_{n_runs}runs_{all_summaries[0].run_id}.md"
        generate_median_report(all_summaries, median_path)
        print("=" * 60)
        print(f"📊 中位数汇总报告（{n_runs} 次）: {median_path}")

        # 打印中位数总览
        case_scores: dict[str, list[float]] = {}
        for s in all_summaries:
            for r in s.results:
                if r.eval_result:
                    case_scores.setdefault(r.case_id, []).append(r.eval_result.total_score)
        medians = [statistics.median(v) for v in case_scores.values() if v]
        if medians:
            print(f"   中位数总分均值: {statistics.mean(medians):.3f}")


def main() -> None:
    """同步入口。"""
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
