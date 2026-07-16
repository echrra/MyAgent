"""
F7 - MySQL 业务错误族 故障注入器。

故障模型（来自 fault_phrases.edgectl.yaml#F7_mysql_business_error）：
    1062 重复键 / 1364 字段无默认值 / 1064 SQL 语法错误。

注入策略：
    - 故障短语行：ERRO，path 是写接口，prefix=database
    - latency_ms 短（错误返回快）
    - Content 必须带（SQL + Error 多行）
"""
from __future__ import annotations

import random
from datetime import datetime

from .base import FILL_PLACEHOLDER, FaultPattern, LogSkeleton

# 业务错误返回快
_LATENCY_BUCKETS: tuple[int, ...] = (8, 15, 32, 78)
_METHOD_CHOICES: tuple[str, ...] = ("POST", "POST", "PUT", "DELETE")


class F7MysqlBusinessError(FaultPattern):
    """F7 MySQL 业务错误族（1062/1364/1064）。"""

    fault_key = "F7_mysql_business_error"

    def build_phrase_line(
        self,
        ts: datetime,
        trace_id: str,
        phrase_hint: str,
        rng: random.Random,
    ) -> LogSkeleton:
        level = "ERRO"     # MySQL 业务错误必然 ERRO
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
            prefix=self.cooccur_prefix,   # database
            caller_path=self._pick_caller_path(rng),
            msg=FILL_PLACEHOLDER,
            content=FILL_PLACEHOLDER,
            is_fault_phrase=True,
            fault_phrase_hint=phrase_hint,
            stack_template_hint=self._pick_stack_template(rng),
        )
