"""
独立的 Critic 评分入口：给已生成的 jsonl 文件打分，不做仿写。

用途：当 Generator 由人工 / 外部 LLM 完成（不是 LLMGenerator API）时，
单独跑 qwen-turbo 评分。

输出格式：
    - 评分摘要打到 stderr
    - 不合格行（score < threshold）的索引 + 原因打到 stdout
    - 可选：把每行评分附加写到 <input>.scored.jsonl，方便人工复查

用法：
    uv run python -m data.synthesizer.critic_only --input data/logs/synth/fault_F1_claude.jsonl
    uv run python -m data.synthesizer.critic_only --input ... --threshold 0.85
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

from loguru import logger

from .llm_critic import LLMCritic


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    return rows


async def _async_main(args: argparse.Namespace) -> None:
    input_path = Path(args.input)
    if not input_path.exists():
        logger.error("输入文件不存在: {}", input_path)
        os._exit(2)

    rows = _load_jsonl(input_path)
    logger.info("载入 {} 行 ← {}", len(rows), input_path)

    critic = LLMCritic(threshold=args.threshold)
    verdicts = await critic.judge(rows)

    # 摘要
    n_pass = sum(1 for v in verdicts if v.score >= args.threshold)
    n_fail = len(verdicts) - n_pass
    logger.info(
        "评分结果: avg={:.3f}  pass={}/{}  fail={}  阈值={:.2f}",
        critic.avg_score, n_pass, len(verdicts), n_fail, args.threshold
    )

    # 分布
    buckets = {"<0.5": 0, "0.5-0.7": 0, "0.7-0.85": 0, "0.85-0.95": 0, "≥0.95": 0}
    for v in verdicts:
        if v.score < 0.5:
            buckets["<0.5"] += 1
        elif v.score < 0.7:
            buckets["0.5-0.7"] += 1
        elif v.score < 0.85:
            buckets["0.7-0.85"] += 1
        elif v.score < 0.95:
            buckets["0.85-0.95"] += 1
        else:
            buckets["≥0.95"] += 1
    logger.info("分布: {}", buckets)

    # 不合格清单
    if n_fail and args.show_fail:
        print("\n--- 不合格行 ---")
        for i, v in enumerate(verdicts):
            if v.score < args.threshold:
                msg_preview = rows[i].get("msg", "")[:60]
                print(f"  [{i:3d}] score={v.score:.2f}  ({v.reason})  msg={msg_preview!r}")

    # 落盘 scored.jsonl
    if args.save_scored:
        out_path = input_path.with_suffix(".scored.jsonl")
        with out_path.open("w", encoding="utf-8") as f:
            for r, v in zip(rows, verdicts, strict=True):
                merged = {**r, "_critic_score": v.score, "_critic_reason": v.reason}
                f.write(json.dumps(merged, ensure_ascii=False) + "\n")
        logger.info("评分明细 → {}", out_path)

    sys.stdout.flush()
    sys.stderr.flush()
    # 与 run_all 一致：直接 _exit 避免 asyncio cleanup hang
    os._exit(0 if n_fail == 0 else 1)


def main() -> int:
    parser = argparse.ArgumentParser(description="给已生成 jsonl 跑 Critic 评分（不仿写）")
    parser.add_argument("--input", required=True, help="待评分 jsonl 路径")
    parser.add_argument("--threshold", type=float, default=0.7)
    parser.add_argument("--show-fail", action="store_true", help="打印不合格行明细")
    parser.add_argument("--save-scored", action="store_true", help="落盘附带评分的 jsonl")
    args = parser.parse_args()

    logger.remove()
    logger.add(sys.stderr, level="INFO", format="<level>{message}</level>")

    asyncio.run(_async_main(args))
    return 0


if __name__ == "__main__":
    sys.exit(main())
