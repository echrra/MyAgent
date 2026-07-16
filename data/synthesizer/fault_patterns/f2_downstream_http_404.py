"""
F2 - 下游 HTTP 404 / 远端错误 故障注入器。

故障模型（来自 fault_phrases.edgectl.yaml#F2_downstream_http_404）：
    本服务调下游 HTTP 接口收到 4xx/5xx，自身被包成 200+errCode 或 5xx。
    陷阱：HTTP 200 不代表业务成功，要看 Response body 的 errorCode。

注入策略：
    - 故障短语行：ERRO 级，path 取 typical_path，status_code 偏向 502/503/200
    - latency_ms 短（远端 404 一般很快返回，几百毫秒）
    - prefix=http，高比例 ERRO 行附带 Content 体（含 stack）
"""
from __future__ import annotations

import random
from datetime import datetime

from .base import FILL_PLACEHOLDER, FaultPattern, LogSkeleton

# 下游 4xx 一般很快返回；偶尔超时升到秒级
_LATENCY_BUCKETS: tuple[int, ...] = (120, 230, 450, 1_200, 3_500)
_METHOD_CHOICES: tuple[str, ...] = ("GET", "POST", "POST", "PUT")


class F2DownstreamHttp404(FaultPattern):
    """F2 下游 HTTP 错误。"""

    fault_key = "F2_downstream_http_404"

    def build_phrase_line(
        self,
        ts: datetime,
        trace_id: str,
        phrase_hint: str,
        rng: random.Random,
    ) -> LogSkeleton:
        # ERRO 主，偶尔 WARN（远端 503 重试场景）
        level = "ERRO" if rng.random() < 0.88 else "WARN"
        status_code = rng.choice(self.cooccur_status_codes)
        latency = rng.choice(_LATENCY_BUCKETS)

        path = self.typical_path
        if rng.random() < 0.15:
            path = path.replace("<resource>", rng.choice(["work", "device", "user", "controltask"]))

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
