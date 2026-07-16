"""
F9 - Go panic / nil pointer 故障注入器。

故障模型（来自 fault_phrases.edgectl.yaml#F9_panic_nil_pointer）：
    业务代码 panic（nil pointer / index out of range / send on closed channel），
    被 recover 中间件接住，写 ERRO，含 goroutine stack。

注入策略：
    - 故障短语行：ERRO，path 是触发 panic 的接口，status_code=500
    - Content 必须带（goroutine 栈）
    - prefix=default
"""
from __future__ import annotations

import random
from datetime import datetime

from .base import FILL_PLACEHOLDER, FaultPattern, LogSkeleton

_LATENCY_BUCKETS: tuple[int, ...] = (5, 12, 35, 80)
_METHOD_CHOICES: tuple[str, ...] = ("GET", "POST", "POST", "PUT")


class F9PanicNilPointer(FaultPattern):
    """F9 Go panic / nil pointer。"""

    fault_key = "F9_panic_nil_pointer"

    def build_phrase_line(
        self,
        ts: datetime,
        trace_id: str,
        phrase_hint: str,
        rng: random.Random,
    ) -> LogSkeleton:
        level = "ERRO"     # panic 必然 ERRO
        status_code = rng.choice(self.cooccur_status_codes)
        latency = rng.choice(_LATENCY_BUCKETS)

        path = self.typical_path
        if rng.random() < 0.2:
            path = path.replace("<resource>", rng.choice(["work", "user", "device", "controltask"]))

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
            content=FILL_PLACEHOLDER,                  # panic 必须有 stack
            is_fault_phrase=True,
            fault_phrase_hint=phrase_hint,
            stack_template_hint=self._pick_stack_template(rng),
        )
