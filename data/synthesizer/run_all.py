"""
数据合成主入口 (W0 最小闭环：当前只跑 F1)。

流程（docs/05 §0 三层架构 - 层 3）：
    1. 加载 fault_phrases.<svc>.yaml + schema.<svc>.yaml
    2. 实例化 FaultPattern，按 N 个时间窗注入骨架
    3. LLMGenerator 批量仿写 msg / Content
    4. LLMCritic 评分；< 阈值的行 Generator 重生成 ≤ 3 次仍不过则丢弃
    5. 合格行写入 data/logs/synth/fault_<id>_<timestamp>.jsonl

用法：
    uv run python -m data.synthesizer.run_all --fault F1 --windows 2 --seed 42

设计取舍：
    - 当前只接 F1；F2-F8 在 docs/09 §2.4 端到端跑通后再补
    - 不引入 trace_id 跨服务透传（W1 RAG 之前用不上）
    - 重生成策略：失败行单独再喂 Generator 一次（不重跑整批），避免雪崩
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from loguru import logger

from .fault_patterns.base import FaultPattern, FaultWindow, LogSkeleton
from .fault_patterns.f1_cascade_timeout import F1CascadeTimeout
from .fault_patterns.f2_downstream_http_404 import F2DownstreamHttp404
from .fault_patterns.f3_kafka_producer_failed import F3KafkaProducerFailed
from .fault_patterns.f4_tos_object_canceled import F4TosObjectCanceled
from .fault_patterns.f5_sql_ctx_canceled import F5SqlCtxCanceled
from .fault_patterns.f6_dns_lookup_failed import F6DnsLookupFailed
from .fault_patterns.f7_mysql_business_error import F7MysqlBusinessError
from .fault_patterns.f8_configsdk_disconnect import F8ConfigsdkDisconnect
from .fault_patterns.f9_panic_nil_pointer import F9PanicNilPointer
from .fault_patterns.f10_token_decode_error import F10TokenDecodeError
from .llm_critic import CriticVerdict, LLMCritic
from .llm_generator import LLMGenerator

# 故障 ID → (类, 默认服务名)
# 服务名映射依据真实生产观察：
#   - F1/F2/F5 业务主流量在 http
#   - F3/F4/F7/F8/F9/F10 大量异步 / 管控类落到 admin
#   - F6 DNS 抖动常见于 watcher 周期性外呼
FAULT_REGISTRY: dict[str, tuple[type[FaultPattern], str]] = {
    "F1":  (F1CascadeTimeout,      "edgectl-backend-http"),
    "F2":  (F2DownstreamHttp404,   "edgectl-backend-http"),
    "F3":  (F3KafkaProducerFailed, "edgectl-admin"),
    "F4":  (F4TosObjectCanceled,   "edgectl-admin"),
    "F5":  (F5SqlCtxCanceled,      "edgectl-backend-http"),
    "F6":  (F6DnsLookupFailed,     "edgectl-backend-watcher"),
    "F7":  (F7MysqlBusinessError,  "edgectl-admin"),
    "F8":  (F8ConfigsdkDisconnect, "edgectl-admin"),
    "F9":  (F9PanicNilPointer,     "edgectl-admin"),
    "F10": (F10TokenDecodeError,   "edgectl-admin"),
}

# seeds 目录（约定与 docs/09 §2.2 一致）
SEEDS_DIR = Path(__file__).parent.parent / "logs" / "seeds"
# 合成结果输出目录
SYNTH_DIR = Path(__file__).parent.parent / "logs" / "synth"


async def _regenerate_failed(
    skeletons: list[LogSkeleton],
    rows: list[dict[str, Any]],
    verdicts: list[CriticVerdict],
    generator: LLMGenerator,
    critic: LLMCritic,
    *,
    max_attempts: int = 3,
) -> tuple[list[dict[str, Any]], list[CriticVerdict], int]:
    """对不合格的行重生成 ≤ max_attempts 次。

    Returns:
        最终 rows、verdicts、被永久丢弃的行数
    """
    discarded = 0
    for attempt in range(2, max_attempts + 1):  # 第 1 次是首发，从第 2 次起算重试
        fail_indices = [i for i, v in enumerate(verdicts) if v.score < critic.threshold]
        if not fail_indices:
            break
        logger.info("重生成 attempt={} 待处理 {} 行", attempt, len(fail_indices))

        retry_skeletons = [skeletons[i] for i in fail_indices]
        retry_rows = await generator.fill(retry_skeletons)
        retry_verdicts = await critic.judge(retry_rows)

        # 覆盖原结果
        for slot, new_row, new_verdict in zip(fail_indices, retry_rows, retry_verdicts, strict=True):
            rows[slot] = new_row
            verdicts[slot] = new_verdict

    # 最终仍不过的行 → 丢弃
    final_pass_rows: list[dict[str, Any]] = []
    final_pass_verdicts: list[CriticVerdict] = []
    for r, v in zip(rows, verdicts, strict=True):
        if v.score >= critic.threshold:
            final_pass_rows.append(r)
            final_pass_verdicts.append(v)
        else:
            discarded += 1
    return final_pass_rows, final_pass_verdicts, discarded


async def synth_one_fault(
    fault_id: str,
    windows: int,
    seed: int,
    output_path: Path,
) -> dict[str, Any]:
    """合成单个故障模式的数据，写 jsonl。

    Returns:
        汇总统计 dict
    """
    if fault_id not in FAULT_REGISTRY:
        raise ValueError(
            f"未注册的 fault_id={fault_id}；当前可用: {list(FAULT_REGISTRY.keys())}"
        )
    fault_cls, service = FAULT_REGISTRY[fault_id]
    rng = random.Random(seed)

    # 短语 yaml 路径按服务名解析
    svc_short = service.split("-")[0]  # edgectl-backend-http → edgectl
    phrases_yaml = SEEDS_DIR / f"fault_phrases.{svc_short}.yaml"
    if not phrases_yaml.exists():
        raise FileNotFoundError(f"找不到 {phrases_yaml}")

    fp = fault_cls(phrases_yaml=phrases_yaml, service=service)
    generator = LLMGenerator(schema_yaml=SEEDS_DIR / f"schema.{svc_short}.yaml")
    critic = LLMCritic()

    # ---------- 注入骨架 ----------
    all_skeletons: list[LogSkeleton] = []
    base_time = datetime(2026, 6, 8, 10, 0, 0, tzinfo=UTC)
    for w in range(windows):
        window = FaultWindow(
            start=base_time.replace(minute=w * 5),
            duration_sec=60,
            service=service,
            phrase_density=0.3,
            total_lines=24,                 # 每窗 24 行（端到端跑通用小量）
            trace_pool_size=3,
            rng=rng,
        )
        all_skeletons.extend(fp.inject(window))
    logger.info("Rule 注入完成: {} 行骨架（{} 个窗口）", len(all_skeletons), windows)

    # ---------- LLM 仿写 ----------
    rows = await generator.fill(all_skeletons)
    logger.info("Generator 完成: {} 行（兜底={}）", len(rows), generator.stats["rows_fallback"])

    # ---------- Critic 评分 ----------
    verdicts = await critic.judge(rows)
    logger.info(
        "Critic 首发: avg={:.3f} pass={}/{}",
        critic.avg_score, critic.stats["rows_pass"], critic.stats["rows_judged"]
    )

    # ---------- 重生成不合格行 ----------
    rows, verdicts, discarded = await _regenerate_failed(
        all_skeletons, rows, verdicts, generator, critic, max_attempts=3
    )
    logger.info("最终: 入库 {} 行，丢弃 {} 行", len(rows), discarded)

    # ---------- 写 jsonl ----------
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    return {
        "fault_id": fault_id,
        "service": service,
        "windows": windows,
        "skeletons": len(all_skeletons),
        "final_rows": len(rows),
        "discarded": discarded,
        "critic_avg_score": critic.avg_score,
        "generator_stats": generator.stats,
        "critic_stats": critic.stats,
        "output": str(output_path),
    }


def _print_summary(result: dict[str, Any]) -> None:
    """端到端摘要——主流程跑完后调用一次。"""
    print("\n" + "=" * 60)
    print(f"✅ 合成完成: fault={result['fault_id']}")
    print(f"   服务:     {result['service']}")
    print(f"   骨架:     {result['skeletons']} 行 / {result['windows']} 窗")
    print(f"   入库:     {result['final_rows']} 行")
    print(f"   丢弃:     {result['discarded']} 行")
    print(f"   Critic:   avg={result['critic_avg_score']:.3f}")
    print(f"   输出:     {result['output']}")
    print("=" * 60)


async def _async_main(args: argparse.Namespace) -> None:
    """async 入口；末尾直接 os._exit 避免 asyncio cleanup hang。"""
    # 支持 --fault all：循环跑全部已注册故障
    if args.fault.lower() == "all":
        all_results: list[dict[str, Any]] = []
        for fid in FAULT_REGISTRY:
            output_path = SYNTH_DIR / f"fault_{fid}_seed{args.seed}.jsonl"
            logger.info("==== 开跑 {} ====", fid)
            result = await synth_one_fault(
                fault_id=fid,
                windows=args.windows,
                seed=args.seed,
                output_path=output_path,
            )
            _print_summary(result)
            all_results.append(result)
        # 全集总览
        print("\n" + "#" * 60)
        print("# 全集合成总览")
        print("#" * 60)
        for r in all_results:
            print(
                f"  {r['fault_id']:4s}  入库 {r['final_rows']:3d}  "
                f"丢弃 {r['discarded']:2d}  Critic={r['critic_avg_score']:.3f}"
            )
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(0)

    output_path = args.output or (
        SYNTH_DIR / f"fault_{args.fault}_seed{args.seed}.jsonl"
    )

    result = await synth_one_fault(
        fault_id=args.fault,
        windows=args.windows,
        seed=args.seed,
        output_path=output_path,
    )
    _print_summary(result)
    # LiteLLM Router 内部 httpx 连接池 + 后台 task 在 asyncio.run() 退出阶段
    # 无法主动 close → 进程会挂住数十秒。CLI 一次性任务直接 _exit 兜底。
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)


def main() -> int:
    parser = argparse.ArgumentParser(description="合成一个或全部故障模式的日志数据")
    parser.add_argument(
        "--fault",
        default="F1",
        help="故障 ID（F1..F10 或 all 跑全集）",
    )
    parser.add_argument("--windows", type=int, default=2, help="时间窗个数")
    parser.add_argument("--seed", type=int, default=42, help="随机种子")
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="输出路径；不传则写到 data/logs/synth/fault_<id>_<ts>.jsonl",
    )
    args = parser.parse_args()

    logger.remove()
    logger.add(sys.stderr, level="INFO", format="<level>{message}</level>")

    asyncio.run(_async_main(args))
    # 正常路径不会到这里（_async_main 内部 os._exit）；保留作 mypy 安抚
    return 0


if __name__ == "__main__":
    sys.exit(main())
