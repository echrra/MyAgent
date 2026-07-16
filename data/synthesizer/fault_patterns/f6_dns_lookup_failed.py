"""
F6 - DNS 解析失败 + 多次重试 故障注入器。

故障模型（来自 fault_phrases.edgectl.yaml#F6_dns_lookup_failed）：
    向某域名 dial 时 DNS 解析失败，伴随退避重试 attempt=1/2/3，delay=1/2/4s。

注入策略：
    - 故障短语行：前几次 attempt WARN，最终失败 ERRO
    - 异步链路：无 path / status_code
    - prefix=default
"""
from __future__ import annotations

import random
from datetime import datetime

from .base import FILL_PLACEHOLDER, FaultPattern, LogSkeleton


class F6DnsLookupFailed(FaultPattern):
    """F6 DNS 解析失败。"""

    fault_key = "F6_dns_lookup_failed"

    def build_phrase_line(
        self,
        ts: datetime,
        trace_id: str,
        phrase_hint: str,
        rng: random.Random,
    ) -> LogSkeleton:
        # 重试关键词偏 WARN；最终 lookup failed 偏 ERRO
        hint_lower = phrase_hint.lower()
        if "retry" in hint_lower or "attempt" in hint_lower:
            level = "WARN" if rng.random() < 0.8 else "ERRO"
        else:
            level = "ERRO" if rng.random() < 0.7 else "WARN"

        return LogSkeleton(
            time=ts,
            level=level,
            trace_id=trace_id,
            service=self.service,
            # DNS 链路无 HTTP path
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
