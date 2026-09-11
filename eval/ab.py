"""成对 A/B 对照 —— 同一批 case、同一配置，只翻一个变量，自动出 diff 报告。

为什么要这个模块：
Wiki 有效性此前靠「W8.6 无 Wiki vs Wiki 两轮」历史对比，但中间隔了 vendor 切换、
timeout 调整、知识库整形等多个混杂变量，只能写「当作一致」来妥协。成对 A/B 保证
两轮之间只有一个字段不同——改的那一个叫「处理变量」，其余全部锁死。产出的 Δ 才
是干净的因果数字。

原理：
  1. 用 python -m eval.runner 正常跑两轮（A=基线，B=实验），每轮自动带指纹
  2. 加载两轮的报告 + 指纹 JSON
  3. 先 diff 指纹：如果只翻了一个变量，指纹 diff 应刚好只有该字段
  4. 再 diff 分数：逐 case 对比 + 总览指标对比 + 波动 case 列表
  5. 输出 Markdown diff 报告

用法（CLI）：
  # 先跑两轮正常评测（各自带指纹），再对比
  python -m eval.ab \\
    --report-a eval/reports/eval_20260903_xxx.md \\
    --report-b eval/reports/eval_20260904_yyy.md
"""

from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path
from typing import Any

from .fingerprint import ExperimentFingerprint, diff_fingerprints

# ====================== 数据解析 ======================


def _load_fingerprint_for_report(report_path: Path) -> dict[str, Any] | None:
    """尝试加载与同轮评测关联的指纹 JSON。

    约定：报告路径 eval/reports/eval_XXX_YYY.md 对应的指纹为
    eval/reports/fingerprint_eval_XXX_YYY.json。

    Returns:
        指纹字典；文件不存在返回 None
    """
    # 从报告文件名提取 run_id（去掉 .md 后缀）
    run_id = report_path.stem  # e.g. "eval_20260903_053000_abc123"
    fp_path = report_path.parent / f"fingerprint_{run_id}.json"
    if not fp_path.exists():
        return None
    try:
        return json.loads(fp_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _parse_report_md(report_path: Path) -> dict[str, Any]:
    """从 Markdown 报告中提取结构化数据。

    解析报告的 4 个 section：
      - 总览表 → overall metrics
      - 按故障类型表 → per-pattern scores
      - 按难度表 → per-difficulty scores
      - 失败 case 表 → failed case details

    Returns:
        {"overall": {...}, "patterns": {...}, "difficulty": {...}, "failed_cases": [...]}
    """
    text = report_path.read_text(encoding="utf-8")
    lines = text.split("\n")

    result: dict[str, Any] = {
        "overall": {},
        "patterns": {},
        "difficulty": {},
        "failed_cases": [],
        "run_id": "",
    }

    # 提取 run_id（从标题行）
    for line in lines:
        if line.startswith("# Eval Report:"):
            result["run_id"] = line.split(":", 1)[1].strip()
            break

    current_section = ""
    for line in lines:
        line = line.strip()

        # 跟踪当前段落
        if line.startswith("## "):
            current_section = line.lstrip("# ").strip()
            continue

        # 跳过非表格行
        if not line.startswith("|") or "---" in line:
            continue

        cells = [c.strip() for c in line.split("|") if c.strip()]
        if len(cells) < 2:
            continue

        if current_section == "总览":
            # | 指标 | 数值 |
            if len(cells) >= 2 and cells[0] != "指标":
                try:
                    result["overall"][cells[0]] = float(cells[1].rstrip("%"))
                except (ValueError, IndexError):
                    pass

        elif current_section.startswith("按故障类型"):
            # | 类型 | Case数 | 均分 | 最低分case |
            if len(cells) >= 4 and cells[0] != "类型":
                try:
                    result["patterns"][cells[0]] = {
                        "cases": int(cells[1]),
                        "avg": float(cells[2]),
                        "worst": cells[3],
                    }
                except (ValueError, IndexError):
                    pass

        elif current_section == "按难度":
            # | 难度 | Case数 | 均分 |
            if len(cells) >= 3 and cells[0] != "难度":
                try:
                    result["difficulty"][cells[0]] = {
                        "cases": int(cells[1]),
                        "avg": float(cells[2]),
                    }
                except (ValueError, IndexError):
                    pass

        elif current_section.startswith("失败 case"):
            # | ID | 类型 | 难度 | 总分 | 失分原因 |
            # 注意：最外层的 split("|") 会产生首尾空串，cells 过滤后索引 0-based
            if len(cells) >= 5 and cells[0] != "ID":
                try:
                    result["failed_cases"].append({
                        "id": cells[0],
                        "pattern": cells[1],
                        "difficulty": cells[2],
                        "score": float(cells[3]),
                        "reasons": cells[4],
                    })
                except (ValueError, IndexError):
                    pass

    return result


# ====================== Diff 计算 ======================


def compute_diff(
    report_a: dict[str, Any],
    report_b: dict[str, Any],
    fp_a: dict[str, Any] | None = None,
    fp_b: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """计算两轮评测的差异。

    Returns:
        {
            "fingerprint_diffs": [(field, val_a, val_b), ...],
            "overall_diff": {metric: (val_a, val_b, delta), ...},
            "pattern_diffs": {pattern: (avg_a, avg_b, delta), ...},
            "difficulty_diffs": {diff: (avg_a, avg_b, delta), ...},
            "case_flip_flops": [{case_id, score_a, score_b, delta}, ...],  # 一方过线一方不过
            "failed_cases_a": [...],
            "failed_cases_b": [...],
            "newly_failed": [...],   # B 失败但 A 未失败
            "newly_passed": [...],   # B 通过但 A 失败
        }
    """
    diff: dict[str, Any] = {
        "fingerprint_diffs": [],
        "overall_diff": {},
        "pattern_diffs": {},
        "difficulty_diffs": {},
        "case_flip_flops": [],
        "failed_cases_a": report_a.get("failed_cases", []),
        "failed_cases_b": report_b.get("failed_cases", []),
        "newly_failed": [],
        "newly_passed": [],
    }

    # 1. 指纹 diff
    if fp_a and fp_b:
        fp_obj_a = ExperimentFingerprint(**{
            k: v for k, v in fp_a.items()
            if k in ExperimentFingerprint.__dataclass_fields__
        })
        fp_obj_b = ExperimentFingerprint(**{
            k: v for k, v in fp_b.items()
            if k in ExperimentFingerprint.__dataclass_fields__
        })
        diff["fingerprint_diffs"] = diff_fingerprints(fp_obj_a, fp_obj_b)

    # 2. 总览指标 diff
    for key in set(report_a["overall"].keys()) | set(report_b["overall"].keys()):
        va = report_a["overall"].get(key, 0.0)
        vb = report_b["overall"].get(key, 0.0)
        diff["overall_diff"][key] = (va, vb, vb - va)

    # 3. 按故障类型 diff
    for pat in set(report_a["patterns"].keys()) | set(report_b["patterns"].keys()):
        pa = report_a["patterns"].get(pat, {}).get("avg", 0.0)
        pb = report_b["patterns"].get(pat, {}).get("avg", 0.0)
        diff["pattern_diffs"][pat] = (pa, pb, pb - pa)

    # 4. 按难度 diff
    for d in set(report_a["difficulty"].keys()) | set(report_b["difficulty"].keys()):
        da = report_a["difficulty"].get(d, {}).get("avg", 0.0)
        db = report_b["difficulty"].get(d, {}).get("avg", 0.0)
        diff["difficulty_diffs"][d] = (da, db, db - da)

    # 5. 失败 case 差集
    failed_a_ids = {c["id"] for c in diff["failed_cases_a"]}
    failed_b_ids = {c["id"] for c in diff["failed_cases_b"]}
    diff["newly_failed"] = [
        c for c in diff["failed_cases_b"] if c["id"] not in failed_a_ids
    ]
    diff["newly_passed"] = [
        c for c in diff["failed_cases_a"] if c["id"] not in failed_b_ids
    ]

    return diff


# ====================== 报告生成 ======================


def format_diff_report(
    diff: dict[str, Any],
    label_a: str = "A",
    label_b: str = "B",
) -> str:
    """生成 Markdown diff 报告。"""
    lines = [
        f"# A/B 对比报告: {label_a} vs {label_b}",
        "",
    ]

    # --- 指纹 diff ---
    fp_diffs = diff["fingerprint_diffs"]
    if fp_diffs:
        lines.append("## 实验指纹差异")
        lines.append("")
        lines.append("| 字段 | A | B |")
        lines.append("|---|---|---|")
        for field_name, va, vb in fp_diffs:
            # 跳过 collected_at（时间戳总是不同）
            if field_name == "collected_at":
                continue
            lines.append(f"| {field_name} | `{va}` | `{vb}` |")
        lines.append("")
        # 如果有非环境变量差异（如 timeout/concurrency），标注出来
        non_env_diffs = [
            (f, va, vb) for f, va, vb in fp_diffs
            if f not in ("collected_at", "git_dirty")
        ]
        if non_env_diffs:
            lines.append(
                f"> ⚠️ **变量混杂警告**：除目标变量外还有 {len(non_env_diffs)} 个指纹字段不同，"
                "因果归因需谨慎。"
            )
            lines.append("")
    else:
        lines.append("## 实验指纹差异")
        lines.append("")
        lines.append("无差异（或未提供指纹文件）。")
        lines.append("")

    # --- 总览指标 ---
    lines.append("## 总览指标对比")
    lines.append("")
    lines.append(f"| 指标 | {label_a} | {label_b} | Δ |")
    lines.append("|---|---|---|---|")
    for metric, (va, vb, delta) in sorted(diff["overall_diff"].items()):
        sign = "+" if delta > 0 else ""
        lines.append(f"| {metric} | {va:.3f} | {vb:.3f} | {sign}{delta:.3f} |")
    lines.append("")

    # --- 按故障类型 ---
    if diff["pattern_diffs"]:
        lines.append("## 按故障类型对比")
        lines.append("")
        lines.append(f"| 类型 | {label_a} | {label_b} | Δ |")
        lines.append("|---|---|---|---|")
        for pat, (va, vb, delta) in sorted(diff["pattern_diffs"].items()):
            sign = "+" if delta > 0 else ""
            lines.append(f"| {pat} | {va:.3f} | {vb:.3f} | {sign}{delta:.3f} |")
        lines.append("")

    # --- 按难度 ---
    if diff["difficulty_diffs"]:
        lines.append("## 按难度对比")
        lines.append("")
        lines.append(f"| 难度 | {label_a} | {label_b} | Δ |")
        lines.append("|---|---|---|---|")
        for d, (va, vb, delta) in sorted(diff["difficulty_diffs"].items()):
            sign = "+" if delta > 0 else ""
            lines.append(f"| {d} | {va:.3f} | {vb:.3f} | {sign}{delta:.3f} |")
        lines.append("")

    # --- 新增失败/新增通过 ---
    newly_failed = diff["newly_failed"]
    newly_passed = diff["newly_passed"]

    if newly_failed:
        lines.append(f"## 新增失败 case（{label_b} 失败 / {label_a} 通过）— {len(newly_failed)} 条")
        lines.append("")
        lines.append("| ID | 类型 | 难度 | 总分 | 失分原因 |")
        lines.append("|---|---|---|---|---|")
        for c in newly_failed:
            lines.append(
                f"| {c['id']} | {c['pattern']} | {c['difficulty']} "
                f"| {c['score']:.3f} | {c['reasons']} |"
            )
        lines.append("")

    if newly_passed:
        lines.append(f"## 新增通过 case（{label_b} 通过 / {label_a} 失败）— {len(newly_passed)} 条")
        lines.append("")
        lines.append("| ID | 类型 | 难度 | 总分 | 失分原因 |")
        lines.append("|---|---|---|---|---|")
        for c in newly_passed:
            lines.append(
                f"| {c['id']} | {c['pattern']} | {c['difficulty']} "
                f"| {c['score']:.3f} | {c['reasons']} |"
            )
        lines.append("")

    if not newly_failed and not newly_passed:
        lines.append("## 失败 case 变化")
        lines.append("")
        lines.append("两轮失败 case 完全一致。")
        lines.append("")

    return "\n".join(lines)


# ====================== CLI ======================


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="A/B 对比：两份评测报告的 diff")
    parser.add_argument("--report-a", type=str, required=True, help="基线报告路径 (A)")
    parser.add_argument("--report-b", type=str, required=True, help="实验报告路径 (B)")
    parser.add_argument("--label-a", type=str, default="A", help="A 的标签名")
    parser.add_argument("--label-b", type=str, default="B", help="B 的标签名")
    parser.add_argument(
        "--output", type=str, default=None,
        help="diff 报告输出路径（默认 eval/reports/ab_diff_{label_a}_vs_{label_b}.md）",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    path_a = Path(args.report_a)
    path_b = Path(args.report_b)

    if not path_a.exists():
        print(f"❌ 报告不存在: {path_a}")
        return
    if not path_b.exists():
        print(f"❌ 报告不存在: {path_b}")
        return

    # 解析报告
    data_a = _parse_report_md(path_a)
    data_b = _parse_report_md(path_b)

    label_a = args.label_a or data_a.get("run_id", "A")
    label_b = args.label_b or data_b.get("run_id", "B")

    print(f"📊 对比: {label_a} ({len(data_a.get('failed_cases', []))} 失败) "
          f"vs {label_b} ({len(data_b.get('failed_cases', []))} 失败)")

    # 尝试加载指纹
    fp_a = _load_fingerprint_for_report(path_a)
    fp_b = _load_fingerprint_for_report(path_b)
    if fp_a:
        print(f"  ✅ 加载指纹: {path_a.stem}")
    else:
        print(f"  ⚠️ 未找到指纹: fingerprint_{path_a.stem}.json")
    if fp_b:
        print(f"  ✅ 加载指纹: {path_b.stem}")
    else:
        print(f"  ⚠️ 未找到指纹: fingerprint_{path_b.stem}.json")

    # 计算 diff
    diff = compute_diff(data_a, data_b, fp_a, fp_b)

    # 生成报告
    report_text = format_diff_report(diff, label_a, label_b)

    # 输出
    if args.output:
        out_path = Path(args.output)
    else:
        safe_a = label_a.replace("/", "_").replace(" ", "_")
        safe_b = label_b.replace("/", "_").replace(" ", "_")
        out_path = Path("eval/reports") / f"ab_diff_{safe_a}_vs_{safe_b}.md"

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(report_text, encoding="utf-8")
    print(f"📝 Diff 报告: {out_path}")

    # 打印关键发现
    newly_failed = diff["newly_failed"]
    newly_passed = diff["newly_passed"]
    if newly_failed:
        print(f"  🔴 新增失败: {len(newly_failed)} 条 — "
              f"{', '.join(c['id'] for c in newly_failed[:5])}")
    if newly_passed:
        print(f"  🟢 新增通过: {len(newly_passed)} 条 — "
              f"{', '.join(c['id'] for c in newly_passed[:5])}")

    # 指纹差异摘要
    non_env = [d for d in diff["fingerprint_diffs"] if d[0] != "collected_at"]
    if non_env:
        print(f"  ⚠️ 指纹差异 {len(non_env)} 项: "
              f"{', '.join(d[0] for d in non_env[:5])}")


if __name__ == "__main__":
    main()
