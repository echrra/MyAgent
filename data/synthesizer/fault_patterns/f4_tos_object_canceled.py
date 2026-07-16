"""
F4 - 对象存储 PutObject ctx 取消 故障注入器。

故障模型（来自 fault_phrases.edgectl.yaml#F4_tos_object_canceled）：
    上传大对象时 ctx 被上游取消（用户断开 / 网关超时传导）。
    与 F1 共享 "context canceled" 主轴。

注入策略：
    - 故障短语行：ERRO 主，path 是上传接口，status_code=499/500
    - latency_ms 偏大（已经传了一部分才被打断）
    - prefix=default，Content 带 stack
"""
from __future__ import annotations

import random
from datetime import datetime

from .base import FILL_PLACEHOLDER, FaultPattern, LogSkeleton

# 上传被打断前可能已经传了几秒到几十秒
_LATENCY_BUCKETS: tuple[int, ...] = (2_300, 5_100, 12_400, 28_900)
_METHOD_CHOICES: tuple[str, ...] = ("POST", "PUT")


class F4TosObjectCanceled(FaultPattern):
    """F4 对象存储 ctx 取消。"""

    fault_key = "F4_tos_object_canceled"

    def build_phrase_line(
        self,
        ts: datetime,
        trace_id: str,
        phrase_hint: str,
        rng: random.Random,
    ) -> LogSkeleton:
        level = "ERRO" if rng.random() < 0.9 else "WARN"
        status_code = rng.choice(self.cooccur_status_codes)
        latency = rng.choice(_LATENCY_BUCKETS)

        path = self.typical_path
        if rng.random() < 0.15:
            path = path.replace("<resource>", rng.choice(["work", "thumbnail", "avatar"]))

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
            content=FILL_PLACEHOLDER if level == "ERRO" else None,
            is_fault_phrase=True,
            fault_phrase_hint=phrase_hint,
            stack_template_hint=self._pick_stack_template(rng) if level == "ERRO" else None,
        )
