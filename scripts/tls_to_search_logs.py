"""TLS 真实日志 → agent 合成日志格式适配器。

把 fetch_uat_tls_logs.py 拉到的 TLS jsonl 转成 opsagent.core.tools.real.search_logs
能直接吃的格式，并落到 settings.logs_dir 下、命名匹配 glob `fault_F*_claude.jsonl`。

字段映射（TLS → search_logs 归一化层期望的 raw key）：
    Time              → time
    Level             → Level                     （保留）
    TraceId           → TraceId                   （保留，可能为空）
    __container_name__→ service                   （新增；agent 按 service 过滤）
    Content           → msg（前 N 字符摘要）+ Content（完整原文，作为 detail）
    CallerPath        → CallerPath                （保留，作为 detail 兜底）

精筛策略（默认）：
    - 保留 ERRO / WARN 全部
    - 保留含关键词（--keep-kw，默认含 task_id / MotionTraining / algo 等）的 INFO
    - DEBU（含大量 ORM SQL 日志）默认丢弃

用法：
    uv run python scripts/tls_to_search_logs.py \
        --input data/logs/real/prod/tls/cases/<file>.jsonl \
        --fault-id F99 \
        --case-tag 01KWBYV0_motion_tilt \
        --keep-kw "01KWBYV0WAYVGXJHYFRRAW7KSG,MotionTraining,algo,verifyAlgoTaskStatus,handleTaskFailure"

输出：data/logs/synth/fault_F99_real_01KWBYV0_motion_tilt_claude.jsonl
（命名以 _claude.jsonl 结尾以匹配现有 glob；F99 用作"真实数据"占位编号）
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT))
from opsagent.core.utils.pii_sanitizer import sanitize as _sanitize  # noqa: E402
_DEFAULT_OUT_DIR = _REPO_ROOT / "data" / "logs" / "synth"

_DEFAULT_KEEP_KW = [
    "MotionTraining",
    "algo",
    "verifyAlgoTaskStatus",
    "handleTaskFailure",
    "task_failed",
    "errCode",
]

# 默认 service 改名表：合成评测用 edgectl-*，真实日志是 linkcraft-*；不改 agent 代码先做映射
# 让 search_logs(service="edgectl-backend-watcher", ...) 也能查到真实数据
_DEFAULT_SERVICE_REWRITE = {
    "linkcraft-backend-watcher": "edgectl-backend-watcher",
    "linkcraft-backend-http": "edgectl-backend-http",
    "linkcraft-admin": "edgectl-admin",
    "linkcraft-ugc": "edgectl-backend-http",
}


def _msg_preview(content: str | None, max_chars: int = 200) -> str:
    """从 Content 抽出 msg（首行 + 截短，去多余空白）。"""
    if not content:
        return ""
    first_line = content.splitlines()[0].strip()
    return first_line if len(first_line) <= max_chars else first_line[:max_chars] + "…"


def _should_keep(row: dict, keep_kws: list[str], drop_debu: bool) -> bool:
    """筛选规则：ERRO/WARN 全留；INFO/DEBU 按关键词命中或丢弃。"""
    level = row.get("Level", "")
    if level in {"ERRO", "FATA", "WARN"}:
        return True
    if level == "DEBU" and drop_debu:
        return False
    if not keep_kws:
        return level in {"INFO"}
    haystack = (row.get("Content", "") + " " + row.get("CallerPath", "")).lower()
    return any(kw.lower() in haystack for kw in keep_kws)


def convert(args: argparse.Namespace) -> int:
    src = Path(args.input)
    if not src.exists():
        sys.stderr.write(f"[ERR] 输入文件不存在：{src}\n")
        return 2

    keep_kws = [k.strip() for k in args.keep_kw.split(",") if k.strip()] if args.keep_kw else _DEFAULT_KEEP_KW

    # 解析 --service-rewrite "a=b,c=d"；空串表示用默认表；--no-service-rewrite 表示完全不改
    if args.no_service_rewrite:
        rewrite = {}
    elif args.service_rewrite:
        rewrite = dict(item.split("=", 1) for item in args.service_rewrite.split(",") if "=" in item)
    else:
        rewrite = dict(_DEFAULT_SERVICE_REWRITE)

    out_dir: Path = args.output_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    case_tag = args.case_tag.strip() or src.stem
    # 关键：以 _claude.jsonl 结尾才能匹配 search_logs 的默认 glob
    out_path = out_dir / f"fault_{args.fault_id}_real_{case_tag}_claude.jsonl"

    seen_levels: dict[str, int] = {}
    seen_services: dict[str, int] = {}
    rewrite_hits: dict[str, int] = {}
    written = 0
    dropped = 0

    with src.open("r", encoding="utf-8") as fin, out_path.open("w", encoding="utf-8") as fout:
        for line in fin:
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                continue

            if not _should_keep(raw, keep_kws, drop_debu=args.drop_debu):
                dropped += 1
                continue

            content = raw.get("Content") or ""
            raw_service = raw.get("__container_name__", "")
            mapped_service = rewrite.get(raw_service, raw_service)
            if mapped_service != raw_service:
                rewrite_hits[f"{raw_service}→{mapped_service}"] = rewrite_hits.get(f"{raw_service}→{mapped_service}", 0) + 1

            # 顺序：Content 先整体脱敏，msg 从脱敏后的 Content 再截短
            # 若反过来（先截短再脱敏），200 字截点常把 JWT 切成两截，正则匹配不上会漏
            content_clean = _sanitize(content)
            norm = {
                "time": raw.get("Time", ""),
                "Level": raw.get("Level", ""),
                "TraceId": raw.get("TraceId", ""),
                "service": mapped_service,
                "service_real": raw_service,           # 保留原始名便于追溯
                "msg": _msg_preview(content_clean),
                "Content": content_clean,               # 兜底 detail（search_logs 会读这个）
                "CallerPath": raw.get("CallerPath", ""),
                "namespace": raw.get("__namespace__", ""),
                "image": raw.get("__image_name__", ""),
            }
            fout.write(json.dumps(norm, ensure_ascii=False) + "\n")
            written += 1
            seen_levels[norm["Level"]] = seen_levels.get(norm["Level"], 0) + 1
            seen_services[norm["service"]] = seen_services.get(norm["service"], 0) + 1

    print(f"[done] 输入 {written + dropped} 行 → 写入 {written} 行 / 丢弃 {dropped} 行")
    print(f"       Level 分布：{dict(sorted(seen_levels.items()))}")
    print(f"       Service 分布（改名后）：{dict(sorted(seen_services.items(), key=lambda kv: -kv[1]))}")
    if rewrite_hits:
        print(f"       Service 改名命中：{rewrite_hits}")
    print(f"       输出：{out_path}")
    print(f"       下一步：opsagent search_logs(service=<service-name>, keyword=...) 应能查到这批")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--input", required=True, help="TLS jsonl 文件（fetch_uat_tls_logs.py 的产物）")
    p.add_argument("--fault-id", default="F99", help="占位编号（F99 表示'真实数据'，避开 F1-F10 合成模式）")
    p.add_argument("--case-tag", required=True, help="case 短标记，如 01KWBYV0_motion_tilt")
    p.add_argument(
        "--keep-kw",
        default=",".join(_DEFAULT_KEEP_KW),
        help="INFO 级别保留的关键词（逗号分隔）；ERRO/WARN 始终保留",
    )
    p.add_argument("--drop-debu", action="store_true", default=True, help="丢弃 DEBU 级别（含大量 SQL 日志）")
    p.add_argument("--output-dir", type=Path, default=_DEFAULT_OUT_DIR, help="落盘目录（agent settings.logs_dir）")
    p.add_argument(
        "--service-rewrite",
        default="",
        help="service 改名表 'a=b,c=d'。空串走默认表（linkcraft-* → edgectl-*），让合成评测 service 名能查到真实日志",
    )
    p.add_argument(
        "--no-service-rewrite",
        action="store_true",
        help="禁用 service 改名，service 字段保持 TLS 原值（__container_name__）",
    )
    args = p.parse_args()
    return convert(args)


if __name__ == "__main__":
    sys.exit(main())
