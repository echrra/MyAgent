"""
F10 - Token decode 错误（INFO 级别陷阱） 故障注入器。

故障模型（来自 fault_phrases.edgectl.yaml#F10_token_decode_error）：
    令牌解析失败但日志被错误地写成 INFO，告警按 ERRO 过滤完全漏报。
    经典告警盲区。

注入策略：
    - 故障短语行：**INFO**（陷阱所在），path 是任意接口
    - prefix=default
    - 一般不带 Content（INFO 不写 Stack）
    - latency_ms 较短
"""
from __future__ import annotations

import random
from datetime import datetime

from .base import FILL_PLACEHOLDER, FaultPattern, LogSkeleton

_LATENCY_BUCKETS: tuple[int, ...] = (3, 8, 18, 42)
_METHOD_CHOICES: tuple[str, ...] = ("GET", "GET", "POST", "PUT")


class F10TokenDecodeError(FaultPattern):
    """F10 Token decode 错误（INFO 告警盲区）。"""

    fault_key = "F10_token_decode_error"

    def build_phrase_line(
        self,
        ts: datetime,
        trace_id: str,
        phrase_hint: str,
        rng: random.Random,
    ) -> LogSkeleton:
        level = "INFO"     # ← 陷阱的核心：业务错误写成 INFO
        status_code = rng.choice(self.cooccur_status_codes)
        latency = rng.choice(_LATENCY_BUCKETS)

        path = self.typical_path
        if rng.random() < 0.2:
            path = path.replace("<resource>", rng.choice(["work", "user", "device"]))

        return LogSkeleton(
            time=ts,
            level=level,
            trace_id=trace_id,
            service=self.service,
            path=path,
            status_code=status_code,
            method=rng.choice(_METHOD_CHOICES),
            latency_ms=latency,
            prefix=self.cooccur_prefix,
            caller_path=self._pick_caller_path(rng),
            msg=FILL_PLACEHOLDER,
            content=None,                          # INFO 不写 Stack
            is_fault_phrase=True,
            fault_phrase_hint=phrase_hint,
            stack_template_hint=None,
        )
