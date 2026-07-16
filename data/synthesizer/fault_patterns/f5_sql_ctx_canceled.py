"""
F5 - 复杂 SQL ctx 取消 故障注入器。

故障模型（来自 fault_phrases.edgectl.yaml#F5_sql_ctx_canceled）：
    长查询（多表 JOIN / 子查询 / 大表 COUNT）超 ctx 被取消。
    与 F1 共享 ctx 取消主轴。

注入策略：
    - 故障短语行：ERRO 为主，path 是列表/统计接口，prefix=database
    - latency_ms 接近 ctx 超时（数秒至数十秒）
"""
from __future__ import annotations

import random
from datetime import datetime

from .base import FILL_PLACEHOLDER, FaultPattern, LogSkeleton

_LATENCY_BUCKETS: tuple[int, ...] = (3_200, 5_010, 10_000, 30_005)
_METHOD_CHOICES: tuple[str, ...] = ("GET", "POST")


class F5SqlCtxCanceled(FaultPattern):
    """F5 复杂 SQL ctx 取消。"""

    fault_key = "F5_sql_ctx_canceled"

    def build_phrase_line(
        self,
        ts: datetime,
        trace_id: str,
        phrase_hint: str,
        rng: random.Random,
    ) -> LogSkeleton:
        level = "ERRO" if rng.random() < 0.85 else "WARN"
        status_code = rng.choice(self.cooccur_status_codes)
        latency = rng.choice(_LATENCY_BUCKETS)

        path = self.typical_path
        if rng.random() < 0.2:
            path = path.replace("<resource>", rng.choice(["work", "device", "event"]))

        return LogSkeleton(
            time=ts,
            level=level,
            trace_id=trace_id,
            service=self.service,
            path=path,
            status_code=status_code,
            method=rng.choice(_METHOD_CHOICES),
            latency_ms=latency,
            prefix=self.cooccur_prefix,    # database
            caller_path=self._pick_caller_path(rng),
            msg=FILL_PLACEHOLDER,
            content=FILL_PLACEHOLDER if level == "ERRO" else None,
            is_fault_phrase=True,
            fault_phrase_hint=phrase_hint,
            stack_template_hint=self._pick_stack_template(rng) if level == "ERRO" else None,
        )
