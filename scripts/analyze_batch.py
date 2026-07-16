"""批量真实日志的故障 pattern 分析器。

对 fetch_logs_batch.py 拉下来的 prod/uat jsonl 做统计聚合：
- Level 分布
- 服务（__container_name__）top-K
- CallerPath 去行号 top-K
- Content 首句归一化后 top-K（错误短语族）
- prod ⇄ uat 同短语两环境对比
- 分水岭对比：日期区间 A vs 日期区间 B 的 top 变化（验证"新版本上线"或"新故障族"假设）

归一化把易变量替换成占位符，让 "task 01ABC failed" 和 "task 01XYZ failed" 聚合成同一族：
  ULID / UUID / hex hash → <ID>
  手机号 / IP / 端口 → <PHONE> / <IP>
  JWT token → <JWT>
  ISO 时间戳 → <TIME>
  4+ 位数字 → <N>
  URL query → ?…（避 token 从 query 泄回 pattern）

用法：
    uv run python scripts/analyze_batch.py                  # 默认 top-20，全量分析
    uv run python scripts/analyze_batch.py --top 30
    uv run python scripts/analyze_batch.py --env prod       # 只看 prod
    uv run python scripts/analyze_batch.py --split-day 2026-06-27  # 分水岭对比
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_ROOT_DIR = _REPO_ROOT / "data" / "logs" / "real"


# ---------------------------------------------------------------------------
# 归一化正则（顺序敏感：token/URL/IP 要在 <N> 之前）
# ---------------------------------------------------------------------------

_NORMALIZERS: list[tuple[re.Pattern, str]] = [
    # JWT：eyJxxx.yyy.zzz
    (re.compile(r"eyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"), "<JWT>"),
    # URL query string 全体（防 token/id 从 query 漏出打散 pattern）
    (re.compile(r"\?[^\s\"']+"), "?<Q>"),
    # ISO 8601 timestamp（含毫秒/时区）
    (re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[+-]\d{2}:?\d{2}|Z)?"), "<TIME>"),
    # unix ms 时间戳
    (re.compile(r"\b1[6-9]\d{11}\b"), "<TIME_MS>"),
    # 手机号 (中国)
    (re.compile(r"\b1[3-9]\d{9}\b"), "<PHONE>"),
    # IP:port 或 IP
    (re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?\b"), "<IP>"),
    # ULID (26 位大写字母数字)
    (re.compile(r"\b[0-9A-HJKMNP-TV-Z]{26}\b"), "<ULID>"),
    # UUID
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I), "<UUID>"),
    # 32 位 hex hash（trace_id / md5 / sha1 前 32 字符）
    (re.compile(r"\b[0-9a-f]{32}\b", re.I), "<HEX32>"),
    # 16 位 hex（span_id / short trace）
    (re.compile(r"\b[0-9a-f]{16}\b", re.I), "<HEX16>"),
    # ≥4 位数字（避免动到 statusCode 3 位数）
    (re.compile(r"\b\d{4,}\b"), "<N>"),
]


def _normalize(s: str) -> str:
    """把日志正文归一化成 pattern 串。"""
    if not s:
        return ""
    # 只取首行且截短，避免多行 stack 每帧不同 → 无法聚合
    s = s.splitlines()[0]
    if len(s) > 300:
        s = s[:300] + "…"
    for pat, repl in _NORMALIZERS:
        s = pat.sub(repl, s)
    # 归一化多空格
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _norm_caller(cp: str) -> str:
    """CallerPath 去行号：/foo/bar.go:1535: → /foo/bar.go:N:"""
    if not cp:
        return ""
    return re.sub(r"\.go:\d+:", ".go:N:", cp)


# ---------------------------------------------------------------------------
# 加载 & 遍历
# ---------------------------------------------------------------------------


def _iter_files(env: str) -> list[Path]:
    """收集 data/logs/real/<env>/tls/<topic>/YYYY-MM-DD.jsonl。"""
    if env == "all":
        envs = ["prod", "uat"]
    else:
        envs = [env]
    files = []
    for e in envs:
        base = _ROOT_DIR / e / "tls"
        if not base.exists():
            continue
        for f in base.glob("*/[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9].jsonl"):
            if f.stat().st_size > 0:
                files.append(f)
    return sorted(files)


def _row_env(path: Path) -> str:
    """从路径反推环境名。data/logs/real/<env>/tls/... → env。"""
    parts = path.parts
    idx = parts.index("real")
    return parts[idx + 1]


def _row_day(path: Path) -> str:
    return path.stem  # YYYY-MM-DD


# ---------------------------------------------------------------------------
# 统计
# ---------------------------------------------------------------------------


def _print_top(title: str, counter: Counter, top: int, total: int) -> None:
    print(f"\n=== {title}（top {top}/{len(counter)}，总样本 {total}） ===")
    if not counter:
        print("  （空）")
        return
    for rank, (key, cnt) in enumerate(counter.most_common(top), 1):
        pct = 100.0 * cnt / total if total else 0
        display_key = key if len(key) <= 200 else key[:200] + "…"
        print(f"  [{rank:2d}] {cnt:>6d}  {pct:5.2f}%  {display_key}")


def analyze(args: argparse.Namespace) -> int:
    files = _iter_files(args.env)
    if not files:
        sys.stderr.write(f"[ERR] 未找到 {args.env} 分片\n")
        return 2
    print(f"[analyze] 读入 {len(files)} 个分片文件（env={args.env}）")

    total = 0
    by_level: Counter[str] = Counter()
    by_service: Counter[str] = Counter()
    by_caller: Counter[str] = Counter()
    by_content: Counter[str] = Counter()

    # 分环境
    per_env_content: dict[str, Counter[str]] = defaultdict(Counter)
    per_env_service: dict[str, Counter[str]] = defaultdict(Counter)

    # 分水岭：如果指定 --split-day D，把 day < D 归 A，day >= D 归 B
    split_day = args.split_day
    per_side_content: dict[str, Counter[str]] = defaultdict(Counter)
    per_side_service: dict[str, Counter[str]] = defaultdict(Counter)
    per_side_caller: dict[str, Counter[str]] = defaultdict(Counter)

    for path in files:
        env = _row_env(path)
        day = _row_day(path)
        side = None
        if split_day:
            side = "A_before" if day < split_day else "B_after"
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                total += 1
                lvl = row.get("Level", "")
                svc = row.get("__container_name__", "")
                cp = _norm_caller(row.get("CallerPath", ""))
                pat = _normalize(row.get("Content", ""))

                by_level[lvl] += 1
                if svc:
                    by_service[svc] += 1
                    per_env_service[env][svc] += 1
                if cp:
                    by_caller[cp] += 1
                if pat:
                    by_content[pat] += 1
                    per_env_content[env][pat] += 1

                if side:
                    if svc:
                        per_side_service[side][svc] += 1
                    if cp:
                        per_side_caller[side][cp] += 1
                    if pat:
                        per_side_content[side][pat] += 1

    print(f"[analyze] 共处理 {total} 条日志")

    # ---- 输出主报告 ----
    _print_top("Level 分布", by_level, 10, total)
    _print_top("服务分布（__container_name__）", by_service, args.top, total)
    _print_top("CallerPath 归一化", by_caller, args.top, total)
    _print_top("Content 归一化（故障短语族）", by_content, args.top, total)

    # ---- prod vs uat 对比 ----
    if args.env == "all" and per_env_content:
        print("\n\n=== prod ⇄ uat 同短语对比（并集 top） ===")
        merged = Counter()
        for c in per_env_content.values():
            merged.update(c)
        print(f"  {'排名':>2s}  {'prod':>7s}  {'uat':>7s}  {'合计':>7s}  短语")
        for rank, (key, _) in enumerate(merged.most_common(args.top), 1):
            p = per_env_content.get("prod", Counter()).get(key, 0)
            u = per_env_content.get("uat", Counter()).get(key, 0)
            display_key = key if len(key) <= 150 else key[:150] + "…"
            print(f"  [{rank:2d}]  {p:>7d}  {u:>7d}  {p + u:>7d}  {display_key}")

    # ---- 分水岭 ----
    if split_day and (per_side_content["A_before"] or per_side_content["B_after"]):
        print(f"\n\n=== 分水岭对比：< {split_day} vs ≥ {split_day} ===")
        a = per_side_content["A_before"]
        b = per_side_content["B_after"]
        merged = Counter()
        merged.update(a)
        merged.update(b)
        # 按 B - A 差值排序（取新增最多的短语）
        deltas = [(key, b.get(key, 0) - a.get(key, 0), a.get(key, 0), b.get(key, 0)) for key in merged]
        deltas.sort(key=lambda x: x[1], reverse=True)
        print(f"  {'排名':>2s}  {'A':>6s}  {'B':>6s}  {'ΔB-A':>7s}  短语")
        print("  ---- 新增/放大最多的 top --------------------------------------------------------")
        for rank, (key, delta, aa, bb) in enumerate(deltas[:args.top], 1):
            display_key = key if len(key) <= 130 else key[:130] + "…"
            print(f"  [{rank:2d}]  {aa:>6d}  {bb:>6d}  {delta:>+7d}  {display_key}")
        print("  ---- 消退最多的 top（B-A 最负）-------------------------------------------------")
        for rank, (key, delta, aa, bb) in enumerate(sorted(deltas, key=lambda x: x[1])[:args.top // 2], 1):
            display_key = key if len(key) <= 130 else key[:130] + "…"
            print(f"  [{rank:2d}]  {aa:>6d}  {bb:>6d}  {delta:>+7d}  {display_key}")

    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--env", choices=("prod", "uat", "all"), default="all")
    p.add_argument("--top", type=int, default=20, help="每个维度取 top N")
    p.add_argument("--split-day", default="", help="分水岭日期 YYYY-MM-DD；早于该日 → A，晚于/等于 → B")
    args = p.parse_args()
    return analyze(args)


if __name__ == "__main__":
    sys.exit(main())
