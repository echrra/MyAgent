"""
F8 - 配置中心 SDK 断连 故障注入器。

故障模型（来自 fault_phrases.edgectl.yaml#F8_configsdk_disconnect）:
    configsdk grpc watch 流被服务端 graceful_stop，
    客户端退避重连：delay 4s -> 8s -> 16s。

注入策略：
    - 故障短语行：WARN 主（单条断连不算告警，连续 attempt>=3 才需关注）
    - 无 HTTP 字段（异步 grpc 链路）
    - prefix=default
    - 一般不带 Content（WARN 不写 Stack）
"""
from __future__ import annotations

import random
from datetime import datetime

from .base import FILL_PLACEHOLDER, FaultPattern, LogSkeleton


class F8ConfigsdkDisconnect(FaultPattern):
    """F8 配置中心 SDK 断连。"""

    fault_key = "F8_configsdk_disconnect"

    def build_phrase_line(
        self,
        ts: datetime,
        trace_id: str,
        phrase_hint: str,
        rng: random.Random,
    ) -> LogSkeleton:
        # 主要是 WARN；reconnect successful 是 INFO
        if "successful" in phrase_hint.lower():
            level = "INFO"
        else:
            level = "WARN"

        return LogSkeleton(
            time=ts,
            level=level,
            trace_id=trace_id,
            service=self.service,
            # 异步 grpc 链路：无 HTTP 字段
            path=None,
            status_code=None,
            method=None,
            latency_ms=None,
            prefix=self.cooccur_prefix,
            caller_path=self._pick_caller_path(rng),
            msg=FILL_PLACEHOLDER,
            content=None,                     # WARN 不写 Stack
            is_fault_phrase=True,
            fault_phrase_hint=phrase_hint,
            stack_template_hint=None,
        )
