"""工具抽象层 —— 给所有工具统一套上「校验 / 超时 / 重试 / 截断 / 打点」横切治理。

为什么要这层（W3）：
W1/W2 的工具是裸函数挂在 TOOL_REGISTRY 里，缺少统一治理。一个 LLM Agent 调工具有四类
通病要兜：① LLM 给的入参可能缺字段/类型错；② 工具可能卡死要超时；③ 偶发失败值得重试；
④ 工具吐回的大块文本会污染上下文、撑爆 token。Tool 把这四件事集中处理，业务函数只管干活。

与 tool_exec 的契约（关键，别破坏）：
tool_exec 节点用 `asyncio.to_thread(TOOL_REGISTRY[name], **args)` 调工具，且已负责 latency
计时、异常→success=False、unknown_tool 兜底。所以本层：
- Tool.__call__(**kwargs) 保持「同步、收 kwargs、返回 {data, meta} dict」签名，registry 无缝替换；
- 最终失败时直接 raise，让 tool_exec 继续当唯一的错误记录方，不自己吞成 error dict。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass, field
from typing import Any

from loguru import logger
from pydantic import BaseModel, ValidationError

from opsagent.core.config import settings


class ToolValidationError(ValueError):
    """入参校验失败：LLM 给的参数缺字段或类型不对。不重试。"""


class ToolTimeoutError(TimeoutError):
    """工具执行超时。重试用尽后抛出。"""


# 进程级线程池：超时控制靠 future.result(timeout) 实现。
# 注意 tool_exec 已在 to_thread 里跑工具，这里再嵌一层线程只为「能设超时」——
# 纯同步函数无法被安全中断，超时后底层线程仍在跑（孤儿线程），我们只是不再等它返回。
# 真正可中断要么把工具改 async、要么子进程隔离；对本地 IO/mock 工具，停止等待已够用。
#
# 双池隔离（W8.5）：CPU 密集的检索工具（search_sop/kb_search 内含 embedder+reranker 推理，
# 单次 20-120s）与 0-3ms 的轻量工具（search_logs/change_query 等）分池。否则检索工具会长期
# 占满共享线程槽，把本应瞬时返回的轻量工具也卡到排队超时（实测 0ms 的 change_query 被卡 60s）。
_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="tool")
# 检索专用池：并发实际由 pipeline 的 Semaphore(2) 控制，这里给 2 个槽够用，且与轻量池隔离。
_HEAVY_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="tool-heavy")


def _truncate(obj: Any, max_str: int, max_items: int) -> tuple[Any, bool]:
    """递归截断：长字符串截到 max_str，长列表截到 max_items。

    返回 (截断后的对象, 是否发生过截断)。dict/list 深度遍历，其余类型原样返回。
    """
    if isinstance(obj, str):
        if len(obj) > max_str:
            return obj[:max_str] + f"…(已截断, 原长 {len(obj)})", True
        return obj, False
    if isinstance(obj, dict):
        cut = False
        out = {}
        for k, v in obj.items():
            out[k], c = _truncate(v, max_str, max_items)
            cut = cut or c
        return out, cut
    if isinstance(obj, list):
        cut = False
        kept = obj[:max_items]
        dropped = len(obj) - len(kept)
        out = []
        for v in kept:
            tv, c = _truncate(v, max_str, max_items)
            out.append(tv)
            cut = cut or c
        if dropped > 0:
            out.append(f"…(还有 {dropped} 条已省略)")
            cut = True
        return out, cut
    return obj, False


@dataclass
class Tool:
    """工具包装器：可调用对象，对外等价于一个 `(**kwargs) -> {data, meta}` 的同步函数。

    Args:
        name: 工具名（与 TOOL_REGISTRY 的 key 一致）
        fn: 真正干活的同步函数
        args_model: Pydantic 入参模型；None 表示不校验（直接透传 kwargs）
        description: 工具说明（供后续「工具发现」动态拼 prompt 用）
        timeout_s / max_retries / max_str_chars / max_list_items: 治理参数，默认取 settings
    """

    name: str
    fn: Callable[..., dict[str, Any]]
    args_model: type[BaseModel] | None = None
    description: str = ""
    timeout_s: float = field(default_factory=lambda: settings.tool_timeout_s)
    max_retries: int = field(default_factory=lambda: settings.tool_max_retries)
    max_str_chars: int = field(default_factory=lambda: settings.tool_max_output_chars)
    max_list_items: int = field(default_factory=lambda: settings.tool_max_list_items)
    # heavy=True 的工具走独立检索池（_HEAVY_EXECUTOR），避免饿死轻量工具
    heavy: bool = False

    def __call__(self, **kwargs: Any) -> dict[str, Any]:
        started = time.perf_counter()

        # 1) 入参校验：失败直接抛，不重试（坏参数重试也是坏参数）
        call_args = self._validate(kwargs)

        # 2) 带超时的执行 + 重试
        result, attempts, timed_out = self._run_with_retry(call_args)

        # 3) 输出截断：只动 data，meta 保留并标记 truncated
        result, truncated = self._truncate_result(result)

        # 4) trace 打点（结构化日志；Langfuse 接入点留到 W5，见下方注释）
        latency_ms = int((time.perf_counter() - started) * 1000)
        logger.info(
            f"[tool:{self.name}] keys={list(call_args)} latency={latency_ms}ms "
            f"attempts={attempts} timed_out={timed_out} truncated={truncated}"
        )
        # W5 在此处补 Langfuse span：用 self.name 作 span 名，call_args/result 作 in/out。

        return result

    # ---------- 内部步骤 ----------

    def _validate(self, kwargs: dict[str, Any]) -> dict[str, Any]:
        """Pydantic 校验入参；无模型则原样透传。校验失败抛 ToolValidationError。"""
        if self.args_model is None:
            return kwargs
        try:
            return self.args_model(**kwargs).model_dump()
        except ValidationError as exc:
            raise ToolValidationError(f"{self.name} 入参校验失败: {exc}") from exc

    def _run_with_retry(self, call_args: dict[str, Any]) -> tuple[dict[str, Any], int, bool]:
        """执行 fn，带超时与重试。返回 (结果, 实际尝试次数, 末次是否超时)。"""
        last_exc: Exception | None = None
        total = self.max_retries + 1  # 含首次
        executor = _HEAVY_EXECUTOR if self.heavy else _EXECUTOR  # 检索工具走独立池
        for attempt in range(1, total + 1):
            fut = executor.submit(self.fn, **call_args)
            try:
                return fut.result(timeout=self.timeout_s), attempt, False
            except FutureTimeoutError as exc:
                last_exc = exc
                logger.warning(
                    f"[tool:{self.name}] 第 {attempt}/{total} 次超时（>{self.timeout_s}s）"
                )
                if attempt >= total:
                    raise ToolTimeoutError(
                        f"{self.name} 执行超时（>{self.timeout_s}s, 已试 {total} 次）"
                    ) from exc
            except Exception as exc:
                last_exc = exc
                logger.warning(
                    f"[tool:{self.name}] 第 {attempt}/{total} 次失败: "
                    f"{type(exc).__name__}: {exc}"
                )
                if attempt >= total:
                    raise  # 末次原样抛，交给 tool_exec 记 success=False
        # 理论不可达：循环要么 return 要么 raise
        raise RuntimeError(f"{self.name} 重试逻辑异常") from last_exc

    def _truncate_result(self, result: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        """截断结果的 data 字段。非 {data,meta} 结构则整体截断兜底。"""
        if not isinstance(result, dict) or "data" not in result:
            out, cut = _truncate(result, self.max_str_chars, self.max_list_items)
            return out, cut
        data, cut = _truncate(result["data"], self.max_str_chars, self.max_list_items)
        out = {**result, "data": data}
        if cut:
            meta = dict(out.get("meta") or {})
            meta["truncated"] = True
            out["meta"] = meta
        return out, cut
