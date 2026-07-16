"""Langfuse 可观测集成 —— trace / span / score 的统一入口（适配 v3.15+ SDK）。

设计要点:
- 启动时探测本地 Langfuse，不可用则降级到 Cloud，session 级锁定不再切换
- key 为空时全部 no-op，不影响 Agent 正常运行
- v3 SDK 用 create_trace_id + TraceContext 管理 trace 关联
- start_as_current_span 自动维护嵌套父子关系
- 异常全部吞掉（日志 warning），可观测不能拖垮业务
"""

from __future__ import annotations

import urllib.request
from collections.abc import Generator
from contextlib import contextmanager
from typing import Any

from loguru import logger

# Langfuse 客户端单例
_langfuse_client: Any = None
_initialized: bool = False

# trace_id 缓存：外部 trace_id → langfuse 内部 trace_id（用于 score 关联）
_trace_registry: dict[str, str] = {}


def _probe_local(host: str, timeout_s: float = 1.5) -> bool:
    """探测本地 Langfuse 是否可用（GET /api/public/health）。"""
    url = f"{host.rstrip('/')}/api/public/health"
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            return resp.status == 200
    except Exception:
        return False


def _get_langfuse() -> Any | None:
    """懒初始化 Langfuse 客户端单例。

    优先级：本地自托管 → Cloud 兜底 → no-op。
    探测仅在首次调用时执行一次，结果 session 级锁定。
    """
    global _langfuse_client, _initialized
    if _initialized:
        return _langfuse_client

    _initialized = True
    try:
        from opsagent.core.config import get_settings

        settings = get_settings()

        # 尝试本地 Langfuse
        local_host = settings.langfuse_local_host
        local_pk = settings.langfuse_local_public_key
        local_sk = settings.langfuse_local_secret_key

        if local_host and local_pk and local_sk and _probe_local(local_host):
            from langfuse import Langfuse

            _langfuse_client = Langfuse(
                public_key=local_pk,
                secret_key=local_sk,
                host=local_host,
            )
            logger.info(f"Langfuse 已连接 [本地]: {local_host}")
            return _langfuse_client

        # 本地不可用，降级到 Cloud
        if settings.langfuse_public_key and settings.langfuse_secret_key:
            from langfuse import Langfuse

            _langfuse_client = Langfuse(
                public_key=settings.langfuse_public_key,
                secret_key=settings.langfuse_secret_key,
                host=settings.langfuse_host,
            )
            logger.info(f"Langfuse 已连接 [Cloud 降级]: {settings.langfuse_host}")
            return _langfuse_client

        # 两组 key 都无效
        logger.debug("Langfuse key 未配置，可观测模块 no-op")

    except Exception as e:
        logger.warning(f"Langfuse 初始化失败（降级为 no-op）: {e}")
        _langfuse_client = None

    return _langfuse_client


# ====================== 公开 API ======================


def create_trace(
    trace_id: str,
    session_id: str = "",
    user_id: str = "",
    name: str = "opsagent-run",
    metadata: dict[str, Any] | None = None,
) -> None:
    """创建 trace 并注册 trace_id，供后续 span/score 关联。

    v3 SDK：用 create_trace_id 生成确定性 ID，通过 start_as_current_span
    + TraceContext 建立根 span，session/user 信息放入 metadata。
    """
    client = _get_langfuse()
    if client is None:
        return
    try:
        from langfuse.types import TraceContext

        # 生成确定性 trace_id（基于外部 trace_id 作为 seed）
        internal_id = client.create_trace_id(seed=trace_id)
        _trace_registry[trace_id] = internal_id

        # 创建根 span 锚定 trace（携带 session/user 元数据）
        ctx = TraceContext(trace_id=internal_id)
        meta = metadata or {}
        meta.update({"session_id": session_id, "user_id": user_id})
        span = client.start_span(name=name, trace_context=ctx, metadata=meta)
        span.end()
        logger.debug(f"Langfuse trace 已创建: {name} (trace_id={internal_id})")
    except Exception as e:
        logger.warning(f"Langfuse create_trace 失败: {e}")


@contextmanager
def span_context(
    trace_id: str,
    name: str,
    input_data: Any = None,
) -> Generator[None, None, None]:
    """上下文管理器：进入时 start span，退出时 end span。

    保证恰好 yield 一次（可观测失败不影响业务）。
    """
    client = _get_langfuse()
    if client is None or not trace_id:
        yield
        return

    internal_id = _trace_registry.get(trace_id)
    span = None
    try:
        from langfuse.types import TraceContext

        kwargs: dict[str, Any] = {"name": name, "input": input_data}
        if internal_id:
            kwargs["trace_context"] = TraceContext(trace_id=internal_id)
        span = client.start_span(**kwargs)
    except Exception as e:
        logger.warning(f"Langfuse span 创建失败: {e}")

    try:
        yield
    finally:
        if span:
            try:
                span.end()
            except Exception:
                pass


def update_span(
    trace_id: str,
    name: str,
    output: Any = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    """更新当前 span 的输出。"""
    client = _get_langfuse()
    if client is None:
        return
    try:
        client.update_current_span(output=output, metadata=metadata)
    except Exception as e:
        logger.warning(f"Langfuse update_span 失败: {e}")


def end_trace(trace_id: str) -> None:
    """结束一条 trace，从注册表移除。"""
    _trace_registry.pop(trace_id, None)


def score_trace(
    trace_id: str,
    name: str,
    value: float,
    comment: str = "",
) -> None:
    """给指定 trace 打分 —— 通过缓存的 trace_id 关联。"""
    client = _get_langfuse()
    if client is None:
        return

    internal_id = _trace_registry.get(trace_id)
    if internal_id:
        try:
            client.create_score(
                trace_id=internal_id,
                name=name,
                value=value,
                comment=comment or None,
            )
            return
        except Exception as e:
            logger.warning(f"Langfuse create_score 失败: {e}")

    # 回退：尝试 score_current_trace
    try:
        client.score_current_trace(name=name, value=value, comment=comment or None)
    except Exception as e:
        logger.warning(f"Langfuse score_trace 失败: {e}")


def flush() -> None:
    """手动 flush（评测结束时调用）。"""
    client = _get_langfuse()
    if client is None:
        return
    try:
        client.flush()
    except Exception as e:
        logger.warning(f"Langfuse flush 失败: {e}")
