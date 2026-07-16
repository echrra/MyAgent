"""
导出"骨架 + 仿写提示"到 jsonl，供人工 / 外部 LLM 仿写使用。

设计动机（docs/05 三层架构补丁）：
    Generator 模型可选 API（DeepSeek）或人工填充（Claude 直接 Write）。
    本脚本只跑 Rule 注入这一步，不调用任何 LLM，把骨架 + phrase_hint +
    stack_template_hint 直接落盘，下游可用任意方式完成 msg / Content 填充。

输出文件每行结构：
    {
      "skel": {<to_jsonl_dict 的全部字段，msg/Content 为 <FILL_BY_LLM>>},
      "phrase_hint": "<故障短语原文，含占位符>",
      "stack_template_hint": "<栈帧模板，无则 null>",
      "need_content": true/false,
      "is_fault_phrase": true/false
    }

用法：
    uv run python -m data.synthesizer.export_skeletons --fault F1 --windows 2 --seed 42
    uv run python -m data.synthesizer.export_skeletons --fault all --windows 2 --seed 42
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from datetime import UTC, datetime
from pathlib import Path

from loguru import logger

from .fault_patterns.base import FILL_PLACEHOLDER, FaultWindow
from .run_all import FAULT_REGISTRY, SEEDS_DIR

# 骨架输出目录（与 synth/ 同级）
SKELETON_DIR = Path(__file__).parent.parent / "logs" / "skeletons"


def export_one_fault(fault_id: str, windows: int, seed: int, output_path: Path) -> int:
    """导出单个故障的骨架；返回行数。"""
    if fault_id not in FAULT_REGISTRY:
        raise ValueError(
            f"未注册 fault_id={fault_id}；当前可用: {list(FAULT_REGISTRY.keys())}"
        )
    fault_cls, service = FAULT_REGISTRY[fault_id]
    rng = random.Random(seed)

    svc_short = service.split("-")[0]
    phrases_yaml = SEEDS_DIR / f"fault_phrases.{svc_short}.yaml"
    fp = fault_cls(phrases_yaml=phrases_yaml, service=service)

    base_time = datetime(2026, 6, 8, 10, 0, 0, tzinfo=UTC)
    all_lines = []
    for w in range(windows):
        window = FaultWindow(
            start=base_time.replace(minute=w * 5),
            duration_sec=60,
            service=service,
            phrase_density=0.3,
            total_lines=24,
            trace_pool_size=3,
            rng=rng,
        )
        all_lines.extend(fp.inject(window))

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as f:
        for s in all_lines:
            obj = {
                "skel": s.to_jsonl_dict(),
                "phrase_hint": s.fault_phrase_hint,
                "stack_template_hint": s.stack_template_hint,
                "need_content": s.content == FILL_PLACEHOLDER,
                "is_fault_phrase": s.is_fault_phrase,
            }
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")
    return len(all_lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="导出 Rule 注入后的骨架（不调 LLM）")
    parser.add_argument("--fault", default="F1", help="故障 ID 或 all")
    parser.add_argument("--windows", type=int, default=2)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    logger.remove()
    logger.add(sys.stderr, level="INFO", format="<level>{message}</level>")

    targets = list(FAULT_REGISTRY.keys()) if args.fault.lower() == "all" else [args.fault]
    for fid in targets:
        out = SKELETON_DIR / f"fault_{fid}_skeleton.jsonl"
        n = export_one_fault(fid, args.windows, args.seed, out)
        logger.info("导出 {}: {} 行 → {}", fid, n, out.relative_to(out.parent.parent.parent))
    return 0


if __name__ == "__main__":
    sys.exit(main())
