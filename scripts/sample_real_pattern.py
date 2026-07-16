"""从 data/logs/real/ 抽指定 pattern 的真实日志，落到 data/logs/synth/ 让 search_logs 能吃到。

用法：
    uv run python scripts/sample_real_pattern.py \
        --fault-id F11 --tag requeue_storm \
        --keywords "REQUEUE_STORM,requeueCount,requeued too many" \
        --max 800

    uv run python scripts/sample_real_pattern.py \
        --fault-id F12 --tag stuck_task \
        --keywords "STUCK_TASK,suspected dropped,stuck_task_reconciler" \
        --max 800

    uv run python scripts/sample_real_pattern.py \
        --fault-id F13 --tag 401_mass \
        --keywords "Response check failed,未授权 token,socket.io" \
        --max 800

设计：
- 输入：data/logs/real/<env>/tls/<topic>/*.jsonl 全部扫（默认 prod）
- 过滤：Content 含任一 --keywords（大小写不敏感），命中即收
- 采样：达到 --max 后停止（避免单类日志占满 synth 目录）
- 字段映射与 service-rewrite：与 tls_to_search_logs.py 一致（linkcraft-* → edgectl-*）
- 输出：data/logs/synth/fault_<fault-id>_real_<tag>_claude.jsonl（命名匹配 search_logs 的 glob）
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT))
from opsagent.core.utils.pii_sanitizer import sanitize as _sanitize  # noqa: E402
_REAL_ROOT = _REPO_ROOT / "data" / "logs" / "real"
_SYNTH_DIR = _REPO_ROOT / "data" / "logs" / "synth"

# 与 tls_to_search_logs.py 保持一致的默认改名表
_DEFAULT_SERVICE_REWRITE = {
    "linkcraft-backend-watcher": "edgectl-backend-watcher",
    "linkcraft-backend-http": "edgectl-backend-http",
    "linkcraft-backend-scheduler": "edgectl-backend-scheduler",
    "linkcraft-admin": "edgectl-admin",
    "linkcraft-ugc": "edgectl-backend-http",
}


def _msg_preview(content: str, max_chars: int = 200) -> str:
    if not content:
        return ""
    first_line = content.splitlines()[0].strip()
    return first_line if len(first_line) <= max_chars else first_line[:max_chars] + "…"


def sample(args: argparse.Namespace) -> int:
    envs = [e.strip() for e in args.envs.split(",") if e.strip()]
    kws = [k.strip().lower() for k in args.keywords.split(",") if k.strip()]
    if not kws:
        sys.stderr.write("[ERR] --keywords 不能为空\n")
        return 2

    # 收集所有源 jsonl
    src_files: list[Path] = []
    for e in envs:
        base = _REAL_ROOT / e / "tls"
        if not base.exists():
            continue
        for f in base.glob("*/[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9].jsonl"):
            if f.stat().st_size > 0:
                src_files.append(f)
    src_files = sorted(src_files)
    if not src_files:
        sys.stderr.write(f"[ERR] 未找到 data/logs/real/{envs}/tls/**/*.jsonl\n")
        return 2

    _SYNTH_DIR.mkdir(parents=True, exist_ok=True)
    out_path = _SYNTH_DIR / f"fault_{args.fault_id}_real_{args.tag}_claude.jsonl"

    written = 0
    scanned = 0
    level_dist: Counter[str] = Counter()
    service_dist: Counter[str] = Counter()

    with out_path.open("w", encoding="utf-8") as fout:
        for fp in src_files:
            if written >= args.max:
                break
            with fp.open("r", encoding="utf-8") as fin:
                for line in fin:
                    scanned += 1
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        raw = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    content = raw.get("Content") or ""
                    if not any(kw in content.lower() for kw in kws):
                        continue

                    raw_service = raw.get("__container_name__", "")
                    mapped_service = _DEFAULT_SERVICE_REWRITE.get(raw_service, raw_service)

                    # 强制 PII 脱敏 —— JWT/手机号/邮箱等在喂 LLM 前必须清洗
                    # 顺序：先对 Content 脱敏，msg 从脱敏后的 Content 再截短，避免截断后半截 JWT 逃过正则
                    content_clean = _sanitize(content)
                    msg_clean = _msg_preview(content_clean)

                    norm = {
                        "time": raw.get("Time", ""),
                        "Level": raw.get("Level", ""),
                        "TraceId": raw.get("TraceId", ""),
                        "service": mapped_service,
                        "service_real": raw_service,
                        "msg": msg_clean,
                        "Content": content_clean,
                        "CallerPath": raw.get("CallerPath", ""),
                        "namespace": raw.get("__namespace__", ""),
                    }
                    fout.write(json.dumps(norm, ensure_ascii=False) + "\n")
                    written += 1
                    level_dist[norm["Level"]] += 1
                    service_dist[mapped_service] += 1

                    if written >= args.max:
                        break

    print(f"[done] 扫描 {scanned} 行 / 写入 {written} 行 → {out_path}")
    print(f"       Level 分布：{dict(level_dist)}")
    print(f"       Service 分布：{dict(service_dist)}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--fault-id", required=True, help="占位编号（F11/F12/F13）")
    p.add_argument("--tag", required=True, help="短标记，如 requeue_storm / stuck_task / 401_mass")
    p.add_argument("--keywords", required=True, help="Content 关键词（逗号分隔）；含任一即收，大小写不敏感")
    p.add_argument("--max", type=int, default=800, help="最多写多少条（默认 800，够 search_logs 抽 top-20 有丰富样本）")
    p.add_argument("--envs", default="prod", help="要扫的环境（默认 prod；uat 通常噪音多）")
    return sample(p.parse_args())


if __name__ == "__main__":
    sys.exit(main())
