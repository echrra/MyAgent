"""
F3 - Kafka 生产端失败 / 重试 故障注入器。

故障模型（来自 fault_phrases.edgectl.yaml#F3_kafka_producer_failed）：
    Kafka producer 发送失败 + 退避重试 attempt=N。
    异步链路特征：无 HTTP path / status_code / method / latency_ms。

注入策略：
    - 故障短语行：ERRO/WARN 混合，无 path/status_code
    - prefix=default
    - 同 trace_id 下可能出现"attempt 序列"——交给 LLM 通过 phrase_hint 自然衔接
"""
from __future__ import annotations

import random
from datetime import datetime

from .base import FILL_PLACEHOLDER, FaultPattern, LogSkeleton


class F3KafkaProducerFailed(FaultPattern):
    """F3 Kafka 生产端失败。"""

    fault_key = "F3_kafka_producer_failed"

    def build_phrase_line(
        self,
        ts: datetime,
        trace_id: str,
        phrase_hint: str,
        rng: random.Random,
    ) -> LogSkeleton:
        # 重试中的 attempt=1/2 多为 WARN；最终失败 ERRO
        # 用 phrase_hint 关键词推断（含 retry / attempt → WARN 偏多）
        is_retry = "retry" in phrase_hint.lower() or "attempt" in phrase_hint.lower()
        if is_retry:
            level = "WARN" if rng.random() < 0.7 else "ERRO"
        else:
            level = "ERRO" if rng.random() < 0.85 else "WARN"

        return LogSkeleton(
            time=ts,
            level=level,
            trace_id=trace_id,
            service=self.service,
            # 异步链路：无 HTTP 字段
            path=None,
            status_code=None,
            method=None,
            latency_ms=None,
            prefix=self.cooccur_prefix,
            caller_path=self._pick_caller_path(rng),
            msg=FILL_PLACEHOLDER,
            content=FILL_PLACEHOLDER if level == "ERRO" else None,
            is_fault_phrase=True,
            fault_phrase_hint=phrase_hint,
            stack_template_hint=self._pick_stack_template(rng) if level == "ERRO" else None,
        )
