"""UAT 日志离线拉取 —— 从火山引擎 TOS 拉一批日志样本到本地，给 agent 做真实环境评测。

用法：
    # 1) dry-run（默认）：只 list 不下载，按一级前缀聚合对象数 + 总字节
    uv run python scripts/fetch_uat_logs.py --prefix "" --max-keys 1000

    # 2) 看某前缀下的目录结构（用 delimiter='/' 拿 CommonPrefixes）
    uv run python scripts/fetch_uat_logs.py --prefix "logs/linkcraft-backend/" --tree

    # 3) 真下载（需显式 --execute；--max-bytes 兜底防误操作）
    uv run python scripts/fetch_uat_logs.py \
        --prefix "logs/linkcraft-backend/2026-06-30/" \
        --since 2026-06-30T08:00:00Z --until 2026-06-30T10:00:00Z \
        --max-bytes 200MB --execute

凭证读环境变量（见 .env / .env.example）：
    TOS_ACCESS_KEY / TOS_SECRET_KEY / TOS_ENDPOINT / TOS_BUCKET / TOS_REGION

设计原则：
- 默认 dry-run，永远不会"不小心"下东西
- 一切下载受 --max-bytes 兜底；超过即停（已下文件保留，不回滚）
- list 用 V2 + ContinuationToken 翻页，避免一次性内存爆炸
- 凭证永远不写文件、不打印；脚本结束就丢

注意：list 与 get_object 都会调 TOS API，可能产生少量请求费用。
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

# 延迟导入 tos，便于 --help 在没装依赖时也能跑

_REPO_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_OUT = _REPO_ROOT / "data" / "logs" / "real" / "uat"


# ---------------------------------------------------------------------------
# 参数解析与单位换算
# ---------------------------------------------------------------------------


def _parse_size(s: str) -> int:
    """把 "500MB" / "2GB" / "1024" 这种人类可读体积串成字节数。"""
    s = s.strip().upper()
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(B|KB|MB|GB|TB)?", s)
    if not m:
        raise argparse.ArgumentTypeError(f"无法解析体积：{s}")
    num = float(m.group(1))
    unit = m.group(2) or "B"
    factor = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4}[unit]
    return int(num * factor)


def _parse_iso(s: str | None) -> datetime | None:
    """解析 ISO8601 时间串。空串/None 直接返回 None。"""
    if not s:
        return None
    # 兼容 "2026-06-30T08:00:00Z" 与 "2026-06-30 08:00:00"
    s = s.replace("Z", "+00:00").replace(" ", "T", 1)
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _human_bytes(n: int) -> str:
    """字节数变 1.23MB / 4.5GB 这种。"""
    f = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if f < 1024 or unit == "TB":
            return f"{f:.2f} {unit}" if unit != "B" else f"{int(f)} B"
        f /= 1024
    return f"{f:.2f} TB"  # 兜底


@dataclass
class Args:
    bucket: str
    endpoint: str
    region: str
    ak: str
    sk: str
    prefix: str
    delimiter: str
    max_keys: int
    since: datetime | None
    until: datetime | None
    max_bytes: int
    output: Path
    execute: bool
    tree: bool


def _load_args() -> Args:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--bucket", default=os.getenv("TOS_BUCKET", ""), help="TOS bucket 名，默认读 env TOS_BUCKET")
    p.add_argument("--endpoint", default=os.getenv("TOS_ENDPOINT", ""), help="TOS endpoint，默认读 env TOS_ENDPOINT")
    p.add_argument("--region", default=os.getenv("TOS_REGION", "cn-shanghai"), help="区域，默认 cn-shanghai")
    p.add_argument("--prefix", default="", help="对象前缀（如 logs/linkcraft-backend/）")
    p.add_argument("--delimiter", default="/", help="目录分组分隔符，默认 '/'；置空则平铺")
    p.add_argument("--max-keys", type=int, default=1000, help="dry-run/list 时最多枚举的 key 数（保护，单次最大 1000）")
    p.add_argument("--since", type=_parse_iso, default=None, help="ISO8601；只统计/下载 LastModified >= since 的对象")
    p.add_argument("--until", type=_parse_iso, default=None, help="ISO8601；只统计/下载 LastModified < until 的对象")
    p.add_argument("--max-bytes", type=_parse_size, default=_parse_size("500MB"),
                   help="单次执行下载总量上限，超过即停。默认 500MB")
    p.add_argument("--output", type=Path, default=_DEFAULT_OUT, help="本地落盘目录（按 key 原样建子目录）")
    p.add_argument("--execute", action="store_true", help="真下载；不带此开关只 list 不下载")
    p.add_argument("--tree", action="store_true", help="只打印 CommonPrefixes（一级子目录）便于探索结构")
    ns = p.parse_args()

    ak = os.getenv("TOS_ACCESS_KEY", "")
    sk = os.getenv("TOS_SECRET_KEY", "")
    if not (ns.bucket and ns.endpoint and ak and sk):
        sys.stderr.write(
            "[ERR] 凭证或 bucket/endpoint 缺失。\n"
            "  请在 .env 中设置：TOS_ACCESS_KEY / TOS_SECRET_KEY / TOS_ENDPOINT / TOS_BUCKET\n"
            "  并通过 `set -a; source .env; set +a` 注入到当前 shell 后再跑。\n"
        )
        sys.exit(2)

    return Args(
        bucket=ns.bucket, endpoint=ns.endpoint, region=ns.region,
        ak=ak, sk=sk,
        prefix=ns.prefix, delimiter=ns.delimiter, max_keys=ns.max_keys,
        since=ns.since, until=ns.until,
        max_bytes=ns.max_bytes, output=ns.output,
        execute=ns.execute, tree=ns.tree,
    )


# ---------------------------------------------------------------------------
# TOS 操作
# ---------------------------------------------------------------------------


def _make_client(args: Args):
    """创建 TOS V2 客户端。失败抛 RuntimeError，不打印 ak/sk。"""
    import tos  # type: ignore

    try:
        return tos.TosClientV2(args.ak, args.sk, args.endpoint, args.region)
    except Exception as e:  # pragma: no cover
        raise RuntimeError(f"TOS 客户端初始化失败：{type(e).__name__}: {e}") from e


def _iter_objects(client, args: Args, want_delimiter: bool):
    """翻页拉对象列表。yield (kind, item)，kind ∈ {'object', 'prefix'}。"""
    token = None
    listed = 0
    while True:
        resp = client.list_objects_type2(
            bucket=args.bucket,
            prefix=args.prefix,
            delimiter=args.delimiter if want_delimiter else "",
            max_keys=min(1000, args.max_keys - listed) if args.max_keys else 1000,
            continuation_token=token,
        )
        # 一级"目录"
        for cp in resp.common_prefixes or []:
            yield ("prefix", cp.prefix)
        for obj in resp.contents or []:
            yield ("object", obj)
            listed += 1
            if args.max_keys and listed >= args.max_keys:
                return
        if not resp.is_truncated:
            return
        token = resp.next_continuation_token


def _in_window(last_modified: datetime, args: Args) -> bool:
    if args.since and last_modified < args.since:
        return False
    if args.until and last_modified >= args.until:
        return False
    return True


def cmd_dry_run(client, args: Args) -> None:
    """list + 聚合统计；不下载任何字节。"""
    print(f"[dry-run] bucket={args.bucket} prefix={args.prefix!r} delimiter={args.delimiter!r} max_keys={args.max_keys}")
    if args.since or args.until:
        print(f"[dry-run] 时间窗：since={args.since} until={args.until}")

    by_prefix: dict[str, dict[str, int]] = defaultdict(lambda: {"count": 0, "bytes": 0})
    common_prefixes: list[str] = []
    total_count = 0
    total_bytes = 0

    for kind, item in _iter_objects(client, args, want_delimiter=True):
        if kind == "prefix":
            common_prefixes.append(item)
            continue
        # object
        lm = item.last_modified
        if isinstance(lm, str):
            lm = _parse_iso(lm)
        if lm and not _in_window(lm, args):
            continue
        size = item.size
        # 按 prefix 后第一级聚合：把 key 里 prefix 之后的第一个分隔符前的那截当作分组
        rel = item.key[len(args.prefix):]
        bucket_key = rel.split("/", 1)[0] if "/" in rel else rel
        by_prefix[bucket_key]["count"] += 1
        by_prefix[bucket_key]["bytes"] += size
        total_count += 1
        total_bytes += size

    if args.tree or common_prefixes:
        print("\n— 一级子目录（CommonPrefixes）—")
        for cp in sorted(common_prefixes):
            print(f"  {cp}")

    print("\n— 当前 prefix 内对象聚合（仅前 max_keys 个，时间窗过滤后） —")
    if not by_prefix:
        print("  （空）")
    else:
        for k, v in sorted(by_prefix.items(), key=lambda kv: -kv[1]["bytes"]):
            print(f"  {k:40s}  {v['count']:6d} objs  {_human_bytes(v['bytes']):>12s}")
    print(f"\n— 合计：{total_count} objs / {_human_bytes(total_bytes)}")
    print("\n[dry-run] 0 字节下载。要真拉，加 --execute 并配 --prefix/--since/--until/--max-bytes。")


def cmd_download(client, args: Args) -> None:
    """带保护地下载到本地。"""
    args.output.mkdir(parents=True, exist_ok=True)
    print(f"[download] bucket={args.bucket} prefix={args.prefix!r} -> {args.output}")
    print(f"[download] cap = --max-bytes {_human_bytes(args.max_bytes)}（超额即停）")

    downloaded_count = 0
    downloaded_bytes = 0
    skipped_window = 0
    capped = False

    for kind, item in _iter_objects(client, args, want_delimiter=False):
        if kind != "object":
            continue
        lm = item.last_modified
        if isinstance(lm, str):
            lm = _parse_iso(lm)
        if lm and not _in_window(lm, args):
            skipped_window += 1
            continue

        if downloaded_bytes + item.size > args.max_bytes:
            print(f"[stop] 命中 --max-bytes 上限（已下 {_human_bytes(downloaded_bytes)}），停止拉取。")
            capped = True
            break

        local_path = args.output / item.key
        local_path.parent.mkdir(parents=True, exist_ok=True)
        if local_path.exists() and local_path.stat().st_size == item.size:
            # 已有同尺寸文件，跳过（简易幂等）
            continue

        # 流式下载，避免大文件一次性入内存
        resp = client.get_object(args.bucket, item.key)
        with local_path.open("wb") as fh:
            for chunk in resp:
                fh.write(chunk)
        downloaded_count += 1
        downloaded_bytes += item.size
        print(f"  ✓ {item.key}  ({_human_bytes(item.size)})")

    print(
        f"\n[done] downloaded {downloaded_count} objs / {_human_bytes(downloaded_bytes)};"
        f" 时间窗外跳过 {skipped_window};"
        f" 命中体积上限：{capped}"
    )
    print(f"       落盘目录：{args.output}")


def main() -> int:
    args = _load_args()
    client = _make_client(args)
    try:
        if args.execute:
            cmd_download(client, args)
        else:
            cmd_dry_run(client, args)
    finally:
        try:
            client.close()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
