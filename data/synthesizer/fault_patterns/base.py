"""
故障注入器基类。

设计原则（docs/05 §0 三层架构里"层 3 故障注入"那一节）：
    1. 骨架由 Rule 控制：时间窗、TraceId 连续性、Level/路径/状态码共现关系
    2. 血肉由 LLM 填：每条日志的 msg / Content 文本由 Generator 仿写
    3. 故障窗口外保持 baseline，靠 fault_window_density 控制密度

子类需要：
    - fault_id / description
    - load_phrases()：返回故障短语清单 + 共现约束（从 fault_phrases.<svc>.yaml 抽）
    - inject(window)：在给定时间窗内产出 LogSkeleton 列表（msg/Content 留 <FILL_BY_LLM>）

骨架结构刻意贴近 schema.<svc>.yaml 的字段定义；下游 llm_generator 看到 <FILL_BY_LLM>
占位时就知道这里要让 LLM 补一句符合上下文的自然语言。
"""
from __future__ import annotations

import random
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

# 故障日志里 LLM 待填的占位符；llm_generator 见到这个串就会拉上下文做 few-shot
FILL_PLACEHOLDER = "<FILL_BY_LLM>"


@dataclass
class LogSkeleton:
    """单行日志的"骨架"。

    Rule 注入器只填结构性字段（time/Level/TraceId/path/status_code/...），
    msg 或 Content 字段留 FILL_PLACEHOLDER 等 LLM 仿写。

    设计取舍：
        - 不直接生成最终 jsonl 行，而是先出骨架再交给 Generator 仿写
        - 骨架字段尽量贴近 schema yaml，方便 Critic 用 yaml 自校验
        - 仅保留"约束生成"必需的字段，其余由 LLM 自由发挥
    """
    time: datetime
    level: str                          # DEBU / INFO / WARN / ERRO / FATA
    trace_id: str                       # hex32
    service: str                        # 服务名（schema yaml 的 known_services）
    path: str | None = None             # HTTP 请求路径
    status_code: int | None = None      # HTTP 状态码
    method: str | None = None
    latency_ms: int | None = None
    prefix: str = "default"             # glog 子记录器：default / database / http / algo / scheduler / watcher
    caller_path: str | None = None      # GoFrame glog CallerPath（"<file>:<line>"），ERRO 行常带
    msg: str = FILL_PLACEHOLDER         # 短消息，LLM 仿写
    content: str | None = None          # ERRO 才有的多行体，LLM 仿写（含 Stack 帧）
    # 故障注入元信息（评测时用，不入 jsonl）
    is_fault_phrase: bool = False       # 该行是否承载故障短语
    fault_phrase_hint: str | None = None  # 给 LLM 的短语提示
    stack_template_hint: str | None = None  # 给 LLM 的 Stack 帧模板提示（仅 ERRO+Content 行）

    def to_jsonl_dict(self) -> dict[str, Any]:
        """转 LogHub 风格 jsonl 行（仅写入字段，剔除注入元信息）。"""
        d: dict[str, Any] = {
            "time": self.time.isoformat(timespec="milliseconds"),
            "Level": self.level,
            "TraceId": self.trace_id,
            "Prefix": self.prefix,
            "service": self.service,
            "msg": self.msg,
        }
        # 可选字段：有值才落
        for k, v in [
            ("path", self.path),
            ("status_code", self.status_code),
            ("method", self.method),
            ("latency_ms", self.latency_ms),
            ("CallerPath", self.caller_path),
            ("Content", self.content),
        ]:
            if v is not None:
                d[k] = v
        return d


@dataclass
class FaultWindow:
    """故障注入时间窗。"""
    start: datetime
    duration_sec: int
    service: str
    # 故障短语在窗口内的行占比（来自 synthesis_constraints.fault_window_density）
    phrase_density: float = 0.3
    # 窗口内总日志条数（含正常 baseline）
    total_lines: int = 60               # 默认 1 行/秒
    # 同一窗口共用的 TraceId 池大小（模拟同时多个请求链）
    trace_pool_size: int = 3
    # 随机数发生器（用 seed 传入以可复现）
    rng: random.Random = field(default_factory=random.Random)

    @property
    def end(self) -> datetime:
        return self.start + timedelta(seconds=self.duration_sec)


class FaultPattern(ABC):
    """故障注入器基类。

    生命周期：
        1. __init__ 加载 fault_phrases.<svc>.yaml 中对应模式的短语和共现约束
        2. inject(window) 按 phrase_density 在窗口内分散注入 phrase 行
        3. 返回 list[LogSkeleton]，下游 Generator 仿写 msg / Content

    子类只需实现 fault_key / build_phrase_line / build_baseline_line。
    """

    # 子类必须重写：与 fault_phrases.<svc>.yaml 顶层 key 对齐（如 F1_cascade_timeout）
    fault_key: str = ""

    def __init__(
        self,
        phrases_yaml: Path,
        service: str,
    ) -> None:
        """加载短语 + 共现约束。

        Args:
            phrases_yaml: fault_phrases.<svc>.yaml 路径
            service: 服务名（必须在 schema yaml 的 known_services 里）
        """
        if not self.fault_key:
            raise ValueError(f"{type(self).__name__} 必须设置 fault_key")
        with phrases_yaml.open("r", encoding="utf-8") as f:
            all_phrases = yaml.safe_load(f)
        if self.fault_key not in all_phrases:
            raise KeyError(
                f"yaml {phrases_yaml.name} 缺少 fault_key={self.fault_key}；"
                f"已有: {list(all_phrases.keys())}"
            )
        self.spec: dict[str, Any] = all_phrases[self.fault_key]
        self.baseline_spec: dict[str, Any] = all_phrases.get("normal_baseline", {})
        self.service = service

        # 共现约束字段（子类按需用）
        self.phrases: list[str] = self.spec.get("phrases", [])
        self.cooccur_level: str = self.spec.get("cooccur_level", "ERRO")
        self.cooccur_status_codes: list[int] = self.spec.get("cooccur_status_code", [500])
        self.typical_path: str = self.spec.get("typical_path_pattern", "/api/v1/<r>/<a>")
        # 新增：caller_path 候选 + stack 模板（让 LLM 仿写时有"风格锚"）
        self.caller_paths: list[str] = self.spec.get("caller_paths", [])
        self.stack_templates: list[str] = self.spec.get("stack_templates", [])
        # 新增：Prefix 候选（不同故障可能跨 default/database/http/algo/scheduler/watcher）
        self.cooccur_prefix: str = self.spec.get("cooccur_prefix", "default")

    # ------------------------------------------------------------------
    # 辅助：生成 hex32 TraceId
    # ------------------------------------------------------------------
    @staticmethod
    def _gen_trace_id(rng: random.Random) -> str:
        return "".join(rng.choice("0123456789abcdef") for _ in range(32))

    # ------------------------------------------------------------------
    # 辅助：抽 caller_path 候选（无候选时返回 None）
    # ------------------------------------------------------------------
    def _pick_caller_path(self, rng: random.Random) -> str | None:
        if not self.caller_paths:
            return None
        return rng.choice(self.caller_paths)

    # ------------------------------------------------------------------
    # 辅助：抽 stack_template 候选（无候选时返回 None）
    # ------------------------------------------------------------------
    def _pick_stack_template(self, rng: random.Random) -> str | None:
        if not self.stack_templates:
            return None
        return rng.choice(self.stack_templates)

    # ------------------------------------------------------------------
    # 主入口：在窗口内注入故障骨架
    # ------------------------------------------------------------------
    def inject(self, window: FaultWindow) -> list[LogSkeleton]:
        """按密度在窗口内分散注入 phrase 行 + baseline 行。

        策略：
            1. 共 total_lines 行，phrase_density 比例承载故障短语，其余 baseline
            2. 时间均匀打散到 [start, end] 区间
            3. 用同一组 TraceId 池模拟"多请求并发"
        """
        n_phrase = max(1, int(window.total_lines * window.phrase_density))

        # 准备 TraceId 池 —— 同一 TraceId 出现多行,表示一个请求链路的多个日志条
        trace_pool = [self._gen_trace_id(window.rng) for _ in range(window.trace_pool_size)]

        # 时间均匀打散（带 ±0.5s 抖动避免栅栏感）
        step = window.duration_sec / max(window.total_lines, 1)
        timestamps: list[datetime] = []
        for i in range(window.total_lines):
            jitter = window.rng.uniform(-0.5, 0.5)
            t = window.start + timedelta(seconds=i * step + jitter)
            # 钳到窗口内
            t = max(window.start, min(t, window.end))
            timestamps.append(t)

        # 按时间排序后,前 n_phrase 条不一定就是 phrase；用打乱再切分的方式分散
        indices = list(range(window.total_lines))
        window.rng.shuffle(indices)
        phrase_indices = set(indices[:n_phrase])

        lines: list[LogSkeleton] = []
        for i, ts in enumerate(timestamps):
            trace_id = window.rng.choice(trace_pool)
            if i in phrase_indices:
                phrase_hint = window.rng.choice(self.phrases)
                lines.append(self.build_phrase_line(ts, trace_id, phrase_hint, window.rng))
            else:
                lines.append(self.build_baseline_line(ts, trace_id, window.rng))

        # 时间排序保证 jsonl 输出顺序自然
        lines.sort(key=lambda s: s.time)
        return lines

    # ------------------------------------------------------------------
    # 子类实现点
    # ------------------------------------------------------------------
    @abstractmethod
    def build_phrase_line(
        self,
        ts: datetime,
        trace_id: str,
        phrase_hint: str,
        rng: random.Random,
    ) -> LogSkeleton:
        """构造一条承载故障短语的骨架（msg/Content 字段保留 FILL_PLACEHOLDER）。"""

    def build_baseline_line(
        self,
        ts: datetime,
        trace_id: str,
        rng: random.Random,
    ) -> LogSkeleton:
        """构造一条 baseline 骨架（默认实现：用 normal_baseline.phrases 做 LLM 提示）。

        子类可重写以提供更贴合 F\\* 上下文的 baseline。
        """
        baseline_phrases: list[str] = self.baseline_spec.get("phrases", ["request completed"])
        levels = self.baseline_spec.get("cooccur_level", ["INFO"])
        if isinstance(levels, str):
            levels = [levels]
        status = self.baseline_spec.get("cooccur_status_code", [200])
        return LogSkeleton(
            time=ts,
            level=rng.choice(levels),
            trace_id=trace_id,
            service=self.service,
            path=self.typical_path,
            status_code=rng.choice(status),
            method="GET",
            latency_ms=rng.randint(5, 80),
            msg=FILL_PLACEHOLDER,
            is_fault_phrase=False,
            fault_phrase_hint=rng.choice(baseline_phrases),
        )
