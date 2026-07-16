"""批量扒 prod / uat 日志（火山 TLS）—— 按 7 天 × 多 topic 分片落盘。

设计：
- 默认 dry-run：每个 (env, day, topic) 仅拉首页 100 条做"量级体检"，0 字节真下载
- --execute 才翻页拉全量，受 --max-logs-per-day / --max-total-bytes 双兜底
- 落盘按 (env, topic, 自然 UTC 日) 切片：data/logs/real/<env>/tls/<topic>/<YYYY-MM-DD>.jsonl
- 同尺寸文件存在 → 跳过（简易幂等，重跑只续未完成的）
- 默认 query 排除 Level:DEBU（SQL 噪音占比极高）

用法：
    # dry-run：体检 7 天 × prod+uat 量级（推荐先跑这个）
    uv run python scripts/fetch_logs_batch.py --envs prod,uat --days 7

    # 真拉
    uv run python scripts/fetch_logs_batch.py --envs prod,uat --days 7 \
        --max-logs-per-day 5000 --max-total-bytes 2GB --execute

凭证：从 .env 读 TOS_ACCESS_KEY / TOS_SECRET_KEY / TLS_ENDPOINT / TLS_REGION
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

_REPO_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_OUT_ROOT = _REPO_ROOT / "data" / "logs" / "real"

# 环境 → [(topic_name, topic_id), ...]
# 来源：scripts/fetch_uat_tls_logs.py list-topics --project-id e84d9fe8-... 实测产物
# uat-linkcraft 是单 topic 汇总；prod 拆了一个 backend 长保留 topic
_ENV_TOPICS: dict[str, list[tuple[str, str]]] = {
    "prod": [
        ("prod-linkcraft", "6bca89a6-da50-4d0d-8b1a-d635a76d7e9f"),
        ("prod-linkcraft-backend", "e586a477-7bab-4edf-b857-53d996b9403a"),
    ],
    "uat": [
        ("uat-linkcraft", "0d15b2fe-97b4-4096-8af6-8ee0f389665e"),
    ],
    "sit": [
        ("sit-linkcraft", "b6032384-46e5-4f77-8b06-e6162c7ccc0a"),
    ],
    "dev": [
        ("dev-linkcraft", "7676f68f-58d0-4b67-ac5e-cf4877a1ffb7"),
    ],
}

# 默认 query：排除 DEBU（90% 是 ORM SQL 噪音，对 agent 评测意义低）
_DEFAULT_QUERY = "NOT Level:DEBU"


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def _parse_size(s: str) -> int:
    s = s.strip().upper()
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(B|KB|MB|GB|TB)?", s)
    if not m:
        raise argparse.ArgumentTypeError(f"无法解析体积：{s}")
    num = float(m.group(1))
    unit = m.group(2) or "B"
    factor = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4}[unit]
    return int(num * factor)


def _human_bytes(n: int) -> str:
    f = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if f < 1024 or unit == "TB":
            return f"{f:.2f} {unit}" if unit != "B" else f"{int(f)} B"
        f /= 1024
    return f"{f:.2f} TB"


def _to_ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def _utc_today_floor() -> datetime:
    now = datetime.now(timezone.utc)
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def _iter_day_windows(days: int, until: datetime) -> Iterable[tuple[datetime, datetime, str]]:
    """从 until 向前回溯 N 天，每天一段 [00:00, 24:00) UTC。

    yield (since_dt, until_dt, day_str) 三元组。
    """
    until_floor = until.replace(hour=0, minute=0, second=0, microsecond=0)
    for i in range(days):
        since = until_floor - timedelta(days=i + 1)
        u = until_floor - timedelta(days=i)
        yield since, u, since.strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# TLS 客户端
# ---------------------------------------------------------------------------


def _make_client():
    ak = os.getenv("TOS_ACCESS_KEY", "")
    sk = os.getenv("TOS_SECRET_KEY", "")
    endpoint = os.getenv("TLS_ENDPOINT", "https://tls-cn-shanghai.volces.com")
    region = os.getenv("TLS_REGION", "cn-shanghai")
    if not (ak and sk):
        sys.stderr.write(
            "[ERR] 缺少 TOS_ACCESS_KEY / TOS_SECRET_KEY。请 set -a; source .env; set +a 后重跑。\n"
        )
        sys.exit(2)
    from volcengine.tls.TLSService import TLSService  # type: ignore

    return TLSService(endpoint=endpoint, access_key_id=ak, access_key_secret=sk, region=region)


# ---------------------------------------------------------------------------
# 拉取主体
# ---------------------------------------------------------------------------


@dataclass
class DayResult:
    env: str
    topic_name: str
    day: str
    written: int
    bytes_written: int
    list_over: bool   # True = 时间窗内已穷尽；False = 还有更多没拉
    skipped_existing: bool


def _fetch_day(
    client,
    env: str,
    topic_name: str,
    topic_id: str,
    since: datetime,
    until: datetime,
    day_str: str,
    query: str,
    max_logs: int,
    out_path: Path,
    execute: bool,
    remaining_bytes: int,
) -> DayResult:
    """拉一个 (env, topic, day)，受 max_logs 与 remaining_bytes 双兜底。"""
    from volcengine.tls.tls_requests import SearchLogsRequest  # type: ignore

    if out_path.exists() and out_path.stat().st_size > 0 and execute:
        # 简易幂等：已有同窗口文件且非空 → 跳过
        return DayResult(env, topic_name, day_str, 0, 0, True, skipped_existing=True)

    written = 0
    bytes_written = 0
    context = None
    list_over = False
    page_limit = min(100, max_logs) if max_logs else 100
    pages_to_fetch = 1 if not execute else 10**9   # dry-run 只拉 1 页

    out_path.parent.mkdir(parents=True, exist_ok=True)
    fout = out_path.open("w", encoding="utf-8") if execute else None

    try:
        for _ in range(pages_to_fetch):
            req = SearchLogsRequest(
                topic_id=topic_id,
                query=query,
                start_time=_to_ms(since),
                end_time=_to_ms(until),
                limit=page_limit,
                sort="desc",
                context=context,
            )
            resp = client.search_logs_v2(req)
            r = getattr(resp, "search_result", None) or resp
            page_logs = getattr(r, "logs", None) or []
            if not page_logs:
                list_over = True
                break

            if execute:
                for log in page_logs:
                    line = json.dumps(log, ensure_ascii=False, default=str) + "\n"
                    fout.write(line)
                    written += 1
                    bytes_written += len(line.encode("utf-8"))
                    if max_logs and written >= max_logs:
                        break
                    if bytes_written >= remaining_bytes:
                        break
            else:
                # dry-run：估这一页字节数当本日代表
                for log in page_logs:
                    line = json.dumps(log, ensure_ascii=False, default=str)
                    bytes_written += len(line.encode("utf-8")) + 1
                written += len(page_logs)

            context = getattr(r, "context", None)
            list_over = bool(getattr(r, "list_over", False))
            if list_over or not context:
                break
            if execute and (max_logs and written >= max_logs):
                break
            if execute and bytes_written >= remaining_bytes:
                break
    finally:
        if fout:
            fout.close()

    return DayResult(env, topic_name, day_str, written, bytes_written, list_over, skipped_existing=False)


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--envs", default="prod,uat", help="环境列表（逗号分隔），可选：prod/uat/sit/dev")
    p.add_argument("--days", type=int, default=7, help="向前回溯天数（默认 7）")
    p.add_argument("--until", default="", help="截止 ISO8601；默认=今日 UTC 00:00（取昨天结束）")
    p.add_argument("--query", default=_DEFAULT_QUERY, help=f"TLS 查询语法，默认 {_DEFAULT_QUERY!r}")
    p.add_argument("--max-logs-per-day", type=int, default=5000, help="--execute 时单 (env,topic,day) 上限（默认 5000）")
    p.add_argument("--max-total-bytes", type=_parse_size, default=_parse_size("2GB"), help="--execute 时全任务总字节上限（默认 2GB）")
    p.add_argument("--output-root", type=Path, default=_DEFAULT_OUT_ROOT, help="落盘根目录")
    p.add_argument("--execute", action="store_true", help="不带 = dry-run（每片仅拉 100 条估量）；带上才真写文件")
    args = p.parse_args()

    envs = [e.strip() for e in args.envs.split(",") if e.strip()]
    unknown = [e for e in envs if e not in _ENV_TOPICS]
    if unknown:
        sys.stderr.write(f"[ERR] 未知环境：{unknown}；可选：{list(_ENV_TOPICS)}\n")
        return 2

    until_dt = datetime.fromisoformat(args.until.replace("Z", "+00:00")) if args.until else _utc_today_floor()
    if until_dt.tzinfo is None:
        until_dt = until_dt.replace(tzinfo=timezone.utc)

    client = _make_client()
    print(f"[batch] envs={envs} days={args.days} until={until_dt.isoformat()} query={args.query!r}")
    print(f"[batch] mode={'EXECUTE 真下载' if args.execute else 'DRY-RUN 仅体检'}")
    if args.execute:
        print(f"[batch] 兜底：max-logs-per-day={args.max_logs_per_day}, max-total-bytes={_human_bytes(args.max_total_bytes)}")
    print()

    results: list[DayResult] = []
    total_written = 0
    total_bytes = 0
    capped = False

    for env in envs:
        for topic_name, topic_id in _ENV_TOPICS[env]:
            for since, until, day in _iter_day_windows(args.days, until_dt):
                remaining = max(0, args.max_total_bytes - total_bytes) if args.execute else 10**18
                if args.execute and remaining <= 0:
                    capped = True
                    break
                out_path = args.output_root / env / "tls" / topic_name / f"{day}.jsonl"
                r = _fetch_day(
                    client, env, topic_name, topic_id, since, until, day,
                    query=args.query,
                    max_logs=args.max_logs_per_day if args.execute else 100,
                    out_path=out_path,
                    execute=args.execute,
                    remaining_bytes=remaining,
                )
                results.append(r)
                total_written += r.written
                total_bytes += r.bytes_written
                tag = "skip" if r.skipped_existing else ("done" if r.list_over else "more")
                print(
                    f"  [{tag:4s}] {env}/{topic_name}/{day}: "
                    f"{r.written:>5d} 条, {_human_bytes(r.bytes_written):>10s}"
                )
            if capped:
                break
        if capped:
            break

    print()
    print(f"[summary] 共处理 {len(results)} 个 (env,topic,day) 分片")
    print(f"[summary] 写入/估算总量：{total_written} 条, {_human_bytes(total_bytes)}")
    if not args.execute:
        # 估算全量：每片首页 100 条 + list_over=True 视为该片实际值
        n_unfinished = sum(1 for r in results if not r.list_over)
        print(f"[summary] {n_unfinished}/{len(results)} 个分片首页未拉完（list_over=False，实际更多）")
        print()
        print("[next] 加 --execute 真拉。建议同时配 --max-logs-per-day / --max-total-bytes 兜底。")
    elif capped:
        print(f"[summary] ⚠️ 命中 --max-total-bytes={_human_bytes(args.max_total_bytes)} 上限，部分分片未完成。")
    else:
        print("[summary] ✅ 全部完成。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
