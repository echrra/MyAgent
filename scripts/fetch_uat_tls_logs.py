"""UAT 日志离线拉取（TLS 版）—— 从火山引擎 TLS 日志服务搜索/拉取业务运行时日志。

为什么是这条路：
- linkcraft-backend GoFrame glog 配置 stdout=true、path=""，业务日志只走容器 stdout
- K8s 集群把 stdout 转推到火山 TLS（DescribeProjects 实测能看到 24 个 project）
- 同一套 TOS_ACCESS_KEY / TOS_SECRET_KEY 在 IAM 层就有 TLS 读权限

用法：
    # 1) 列 project（dry-run，发现 UAT 对应 project）
    uv run python scripts/fetch_uat_tls_logs.py list-projects

    # 2) 列某 project 下的 topic
    uv run python scripts/fetch_uat_tls_logs.py list-topics --project-id <pid>

    # 3) 预览搜索（dry-run，只打印前 5 条 + 命中总数）
    uv run python scripts/fetch_uat_tls_logs.py search \
        --topic-id <tid> --query "level:error" \
        --since 2026-06-30T08:00:00Z --until 2026-06-30T10:00:00Z

    # 4) 真拉取（写本地 jsonl；--max-logs 兜底防误操作）
    uv run python scripts/fetch_uat_tls_logs.py search \
        --topic-id <tid> --query "level:error" \
        --since 2026-06-30T08:00:00Z --until 2026-06-30T10:00:00Z \
        --max-logs 5000 --execute

凭证读环境变量（与 TOS 共用一套 IAM AK/SK）：
    TOS_ACCESS_KEY / TOS_SECRET_KEY
    TLS_ENDPOINT（默认 https://tls-cn-shanghai.volces.com）
    TLS_REGION（默认 cn-shanghai）

设计原则：
- 所有"读"动作（list-projects / list-topics / search 不带 --execute）只打印不落盘
- 真拉取受 --max-logs 兜底；时间窗强制 since/until 必填，避免不带条件全量拉
- 凭证永远不写文件、不打印、不入日志
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_OUT = _REPO_ROOT / "data" / "logs" / "real" / "uat" / "tls"


# ---------------------------------------------------------------------------
# 工具：时间解析、人类可读
# ---------------------------------------------------------------------------


def _parse_iso(s: str) -> datetime:
    """解析 ISO8601 → 带时区的 datetime。'2026-06-30T08:00:00Z' / '2026-06-30 08:00:00' 都接受。"""
    s = s.replace("Z", "+00:00").replace(" ", "T", 1)
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _to_ms(dt: datetime) -> int:
    """datetime → epoch 毫秒（TLS SearchLogsV2 入参单位）。"""
    return int(dt.timestamp() * 1000)


def _abridge(value: Any, max_chars: int = 240) -> str:
    """把日志字段截短打印，不污染屏幕。"""
    if not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False, default=str)
    return value if len(value) <= max_chars else value[:max_chars] + "…"


# ---------------------------------------------------------------------------
# TLS 客户端
# ---------------------------------------------------------------------------


def _make_client():
    """创建 TLSService 客户端。失败不打印密钥。"""
    ak = os.getenv("TOS_ACCESS_KEY", "")
    sk = os.getenv("TOS_SECRET_KEY", "")
    endpoint = os.getenv("TLS_ENDPOINT", "https://tls-cn-shanghai.volces.com")
    region = os.getenv("TLS_REGION", "cn-shanghai")
    if not (ak and sk):
        sys.stderr.write(
            "[ERR] 缺少 TOS_ACCESS_KEY / TOS_SECRET_KEY（与 TOS 共用 IAM AK/SK）。\n"
            "  请在 .env 中设置后 set -a; source .env; set +a 再跑。\n"
        )
        sys.exit(2)
    from volcengine.tls.TLSService import TLSService  # type: ignore

    return TLSService(endpoint=endpoint, access_key_id=ak, access_key_secret=sk, region=region)


# ---------------------------------------------------------------------------
# 子命令实现
# ---------------------------------------------------------------------------


def cmd_list_projects(args: argparse.Namespace) -> int:
    """列出当前账号下可见的 TLS project。"""
    from volcengine.tls.tls_requests import DescribeProjectsRequest  # type: ignore

    client = _make_client()
    req = DescribeProjectsRequest(page_number=1, page_size=100)
    resp = client.describe_projects(req)
    # SDK 不同版本属性名有差异，做容错
    projects = getattr(resp, "projects", None) or getattr(resp, "project_infos", []) or []
    print(f"[list-projects] 共 {len(projects)} 个 project（仅展示当前页）")
    for p in projects:
        # 兼容 dict 与对象两种返回
        get = (lambda k, default="": p.get(k, default)) if isinstance(p, dict) else (lambda k, default="": getattr(p, k, default))
        print(f"  {get('ProjectId') or get('project_id'):40s}  name={get('ProjectName') or get('project_name')!r:30s}  region={get('Region') or get('region')}")
    return 0


def cmd_list_topics(args: argparse.Namespace) -> int:
    """列出指定 project 下的 topic。"""
    from volcengine.tls.tls_requests import DescribeTopicsRequest  # type: ignore

    if not args.project_id:
        sys.stderr.write("[ERR] --project-id 必填\n")
        return 2
    client = _make_client()
    req = DescribeTopicsRequest(project_id=args.project_id, page_number=1, page_size=100)
    resp = client.describe_topics(req)
    topics = getattr(resp, "topics", None) or getattr(resp, "topic_infos", []) or []
    print(f"[list-topics] project_id={args.project_id} 共 {len(topics)} 个 topic")
    for t in topics:
        get = (lambda k, default="": t.get(k, default)) if isinstance(t, dict) else (lambda k, default="": getattr(t, k, default))
        topic_id = get("TopicId") or get("topic_id")
        topic_name = get("TopicName") or get("topic_name")
        desc = get("Description") or get("description") or ""
        ttl = get("Ttl") or get("ttl")
        print(f"  {topic_id:40s}  name={topic_name!r:40s}  ttl={ttl}d  desc={_abridge(desc, 80)}")
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    """搜索日志：dry-run 只看前几条；--execute 才真写文件。"""
    from volcengine.tls.tls_requests import SearchLogsRequest  # type: ignore

    if not args.topic_id:
        sys.stderr.write("[ERR] --topic-id 必填\n")
        return 2
    since = _parse_iso(args.since)
    until = _parse_iso(args.until)
    if until <= since:
        sys.stderr.write("[ERR] --until 必须晚于 --since\n")
        return 2

    client = _make_client()
    # 第一次只取 5 条做预览（even when --execute），用真值确认接通
    preview_req = SearchLogsRequest(
        topic_id=args.topic_id,
        query=args.query,
        start_time=_to_ms(since),
        end_time=_to_ms(until),
        limit=5,
        sort="desc",
    )
    preview_resp = client.search_logs_v2(preview_req)
    # SearchLogsV2 返回结构：resp.search_result(SearchResult) 才是数据
    result = getattr(preview_resp, "search_result", None) or preview_resp
    hit_count = getattr(result, "hit_count", None)
    logs_preview = getattr(result, "logs", None) or []
    list_over = getattr(result, "list_over", None)

    print(f"[search] topic={args.topic_id} query={args.query!r} 时间窗={args.since}~{args.until}")
    print(
        f"[search] 本批返回 {len(logs_preview)} 条；"
        f"list_over={list_over}（False = 时间窗内仍有更多日志，需翻页）"
    )
    print("       注意：火山 TLS SearchLogsV2 不直接返回总命中数；要精确量级请用 describe_histogram。")
    for i, log in enumerate(logs_preview, 1):
        line = json.dumps(log, ensure_ascii=False, default=str) if not isinstance(log, str) else log
        print(f"  [{i}] {_abridge(line, 360)}")

    if not args.execute:
        print("\n[dry-run] 未落盘。要真拉，加 --execute 并设置 --max-logs 兜底。")
        return 0

    # 真拉：分页 limit=100/次，直到 hit_count / max_logs / list_over 触发
    out_dir: Path = args.output
    out_dir.mkdir(parents=True, exist_ok=True)
    safe_topic = args.topic_id.replace("/", "_")
    stamp = since.strftime("%Y%m%dT%H%M") + "-" + until.strftime("%Y%m%dT%H%M")
    out_path = out_dir / f"{safe_topic}_{stamp}.jsonl"

    written = 0
    context = None
    while written < args.max_logs:
        page_req = SearchLogsRequest(
            topic_id=args.topic_id,
            query=args.query,
            start_time=_to_ms(since),
            end_time=_to_ms(until),
            limit=min(100, args.max_logs - written),
            sort="desc",
            context=context,
        )
        resp = client.search_logs_v2(page_req)
        page_result = getattr(resp, "search_result", None) or resp
        page_logs = getattr(page_result, "logs", None) or []
        if not page_logs:
            break
        with out_path.open("a", encoding="utf-8") as fh:
            for log in page_logs:
                fh.write(json.dumps(log, ensure_ascii=False, default=str) + "\n")
                written += 1
                if written >= args.max_logs:
                    break
        # 翻页 context；list_over=True 表示没有更多
        context = getattr(page_result, "context", None)
        if not context or getattr(page_result, "list_over", False):
            break

    print(f"\n[done] 写入 {written} 条 → {out_path}")
    if written >= args.max_logs:
        print(f"       命中 --max-logs={args.max_logs} 上限；如需更多请加大或缩小时间窗")
    return 0


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list-projects", help="列出账号下所有 TLS project（确认 UAT 在哪）")

    p_topics = sub.add_parser("list-topics", help="列出某 project 下的 topic")
    p_topics.add_argument("--project-id", required=True)

    p_search = sub.add_parser("search", help="搜索日志（默认 dry-run，只看前 5 条；--execute 才落盘）")
    p_search.add_argument("--topic-id", required=True, help="目标 topic_id（从 list-topics 结果里取）")
    p_search.add_argument("--query", default="*", help="TLS 检索语法，如 'level:error AND service:linkcraft-backend'；默认 '*'")
    p_search.add_argument("--since", required=True, help="ISO8601，如 2026-06-30T08:00:00Z")
    p_search.add_argument("--until", required=True, help="ISO8601；必须晚于 --since")
    p_search.add_argument("--max-logs", type=int, default=2000, help="--execute 时本次最多写多少条（默认 2000）")
    p_search.add_argument("--output", type=Path, default=_DEFAULT_OUT, help="落盘目录（jsonl）")
    p_search.add_argument("--execute", action="store_true", help="不带此开关只 dry-run；带上才真写文件")
    return p


def main() -> int:
    args = _build_parser().parse_args()
    if args.cmd == "list-projects":
        return cmd_list_projects(args)
    if args.cmd == "list-topics":
        return cmd_list_topics(args)
    if args.cmd == "search":
        return cmd_search(args)
    return 2


if __name__ == "__main__":
    sys.exit(main())
