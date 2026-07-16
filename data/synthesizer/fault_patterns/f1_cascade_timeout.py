"""
F1 - 级联超时故障注入器（Rule + LLM 混合路径）。

故障模型（来自 fault_phrases.edgectl.yaml#F1_cascade_timeout）：
    上游慢导致下游 context deadline exceeded，错误经多层 wrap 后返回入口接口。
    典型路径：/api/v1/<resource>/deploy
    共现：level=ERRO, status_code ∈ {200, 500, 504}（注意网关 200 包裹业务失败的陷阱）

注入策略：
    - 故障短语行：ERRO 级，path 取 typical_path，status_code 偏向 504/500/200
    - 同一 TraceId 表示同一请求链路上的多条日志（先收 phrase 行,再收上游 wrap）
    - latency_ms 偏向超时阈值附近（5000ms / 30000ms / 60000ms）

msg / Content 字段保留 <FILL_BY_LLM>，由 llm_generator 看着 phrase_hint 仿写。
"""
from __future__ import annotations

import random
from datetime import datetime

from .base import FILL_PLACEHOLDER, FaultPattern, LogSkeleton

# 超时场景常见的 latency 候选（贴近真实生产观察：5/30/60 秒一档）
_TIMEOUT_LATENCY_BUCKETS: tuple[int, ...] = (5_000, 5_023, 30_010, 60_005)
# 故障窗口的 HTTP method 偏向 POST（deploy 类接口）
_METHOD_CHOICES: tuple[str, ...] = ("POST", "POST", "POST", "PUT")


class F1CascadeTimeout(FaultPattern):
    """F1 级联超时。

    一个典型注入窗口的样貌（time 抖动后近似）：
        T+0s   POST /api/v1/<r>/deploy  trace=A  ERRO  <FILL: 写"failed to wait rota release...">
        T+1s   POST /api/v1/<r>/deploy  trace=B  ERRO  <FILL: 写"ctx deadline exceeded">
        T+2s   GET  /api/v1/<r>/deploy  trace=A  INFO  <FILL: 写正常完成>
        ...

    Critic 校验时按 cooccur_level / cooccur_status_code / typical_path 自校验。
    """

    fault_key = "F1_cascade_timeout"

    def build_phrase_line(
        self,
        ts: datetime,
        trace_id: str,
        phrase_hint: str,
        rng: random.Random,
    ) -> LogSkeleton:
        # ERRO 级别为主，偶尔降级为 WARN 体现"分布尾部"
        level = "ERRO" if rng.random() < 0.92 else "WARN"
        status_code = rng.choice(self.cooccur_status_codes)
        latency = rng.choice(_TIMEOUT_LATENCY_BUCKETS)

        # 90% 走 typical_path，10% 随机替换 resource 子串模拟泛化
        path = self.typical_path
        if rng.random() < 0.1:
            path = path.replace("<resource>", rng.choice(["work", "device", "controltask"]))

        return LogSkeleton(
            time=ts,
            level=level,
            trace_id=trace_id,
            service=self.service,
            path=path,
            status_code=status_code,
            method=rng.choice(_METHOD_CHOICES),
            latency_ms=latency,
            prefix="default" if level == "WARN" else "http",
            caller_path=self._pick_caller_path(rng),
            msg=FILL_PLACEHOLDER,
            # 高比例 ERRO 行附带 Content 体（GoFrame glog 风格的多行错误）
            content=FILL_PLACEHOLDER if level == "ERRO" else None,
            is_fault_phrase=True,
            fault_phrase_hint=phrase_hint,
            stack_template_hint=self._pick_stack_template(rng) if level == "ERRO" else None,
        )
