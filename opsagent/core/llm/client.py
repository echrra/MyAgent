"""LLM 客户端：业务代码唯一的模型调用入口。

设计要点：
- 业务只用别名 (plan / reflect / answer / updater)
- 切模型 = 改 litellm_config.yaml + .env，不动业务代码
- 基于 openai SDK 实现（兼容所有 OpenAI 格式 API：官方/DashScope/DeepSeek/中转站）
- 内置 fallback：主模型异常时自动降级到 fallback 别名
- 支持流式 (stream=True) 用于 Answer 节点 SSE 推送
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import yaml
from loguru import logger
from openai import AsyncOpenAI, OpenAI
import httpx

_CONFIG_PATH = Path(__file__).parent / "litellm_config.yaml"

# 别名 → 模型配置（从 yaml 加载）
_MODEL_MAP: dict[str, dict[str, Any]] = {}
# 别名 → fallback 别名列表
_FALLBACKS: dict[str, list[str]] = {}
# 已初始化标记
_initialized: bool = False
# 客户端缓存：避免每次调用都新建 httpx 客户端和 SSL 上下文
_async_clients: dict[str, AsyncOpenAI] = {}
_sync_clients: dict[str, OpenAI] = {}
# 请求超时：connect 快速失败，read 允许 LLM 生成耗时
_HTTP_TIMEOUT = httpx.Timeout(connect=10.0, read=50.0, write=10.0, pool=10.0)


def _init() -> None:
    """从 yaml 加载模型路由配置（懒初始化，首次调用时执行一次）。"""
    global _initialized
    if _initialized:
        return

    # 确保 .env 变量可被 os.environ.get 读取（pydantic_settings 自动读但不写入 os.environ）
    import os
    from pathlib import Path as _P
    _env_path = _P(__file__).resolve().parents[3] / ".env"
    if _env_path.exists():
        for line in _env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip()
            if key and key not in os.environ:
                os.environ[key] = val

    with _CONFIG_PATH.open("r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    # 解析 model_list → 按 model_name(别名) 建索引
    for entry in config.get("model_list", []):
        alias = entry["model_name"]
        params = entry.get("litellm_params", {})
        # 处理 os.environ/ 引用
        api_key = _resolve_env(params.get("api_key", ""))
        api_base = _resolve_env(params.get("api_base", ""))
        # 去掉 openai/ 前缀得到真实模型名
        model = params.get("model", "")
        if model.startswith("openai/"):
            model = model[len("openai/"):]
        elif "/" in model:
            model = model.split("/", 1)[1]

        _MODEL_MAP[alias] = {
            "model": model,
            "api_key": api_key,
            "api_base": api_base,
            "temperature": params.get("temperature"),
        }

    # 解析 fallbacks
    for fb_entry in config.get("router_settings", {}).get("fallbacks", []):
        for alias, targets in fb_entry.items():
            _FALLBACKS[alias] = targets

    _initialized = True
    logger.info(f"LLM Router 初始化完成，可用别名: {sorted(_MODEL_MAP.keys())}")


def _resolve_env(value: str) -> str:
    """解析 'os.environ/VAR_NAME' 引用。"""
    if isinstance(value, str) and value.startswith("os.environ/"):
        import os
        var_name = value[len("os.environ/"):]
        return os.environ.get(var_name, "")
    return value


def _get_client(alias: str) -> tuple[AsyncOpenAI, str, float | None]:
    """获取指定别名的异步客户端 + 模型名 + 默认温度。复用已有客户端，避免反复新建 httpx/SSL。"""
    _init()
    cfg = _MODEL_MAP.get(alias)
    if not cfg:
        raise ValueError(f"未知模型别名: {alias}，可用: {sorted(_MODEL_MAP.keys())}")

    if alias not in _async_clients:
        base_url = cfg["api_base"]
        if base_url and not base_url.endswith("/v1"):
            base_url = base_url.rstrip("/") + "/v1"
        _async_clients[alias] = AsyncOpenAI(
            api_key=cfg["api_key"], base_url=base_url,
            timeout=_HTTP_TIMEOUT, max_retries=0,
        )

    return _async_clients[alias], cfg["model"], cfg.get("temperature")


def _get_sync_client(alias: str) -> tuple[OpenAI, str, float | None]:
    """获取指定别名的同步客户端 + 模型名 + 默认温度。复用已有客户端，避免反复新建 httpx/SSL。"""
    _init()
    cfg = _MODEL_MAP.get(alias)
    if not cfg:
        raise ValueError(f"未知模型别名: {alias}，可用: {sorted(_MODEL_MAP.keys())}")

    if alias not in _sync_clients:
        base_url = cfg["api_base"]
        if base_url and not base_url.endswith("/v1"):
            base_url = base_url.rstrip("/") + "/v1"
        _sync_clients[alias] = OpenAI(
            api_key=cfg["api_key"], base_url=base_url,
            timeout=_HTTP_TIMEOUT, max_retries=0,
        )

    return _sync_clients[alias], cfg["model"], cfg.get("temperature")


# ====================== 公开 API（保持与旧版签名一致）======================


def get_router() -> Any:
    """兼容旧调用点（query_rewriter 等用 get_router().completion）。

    返回一个鸭子类型对象，支持 .completion() 同步调用。
    """
    _init()
    return _SyncRouter()


class _SyncRouter:
    """同步路由器（供 query_rewriter 等在 worker 线程中调用）。"""

    def completion(
        self,
        model: str,
        messages: list[dict[str, Any]],
        **kwargs: Any,
    ) -> Any:
        """同步非流式调用。"""
        client, real_model, default_temp = _get_sync_client(model)
        params: dict[str, Any] = {"model": real_model, "messages": messages}
        # temperature：调用方传入 > yaml 默认 > 不传
        temp = kwargs.pop("temperature", None)
        if temp is not None:
            params["temperature"] = temp
        elif default_temp is not None:
            params["temperature"] = default_temp
        if "max_tokens" in kwargs:
            params["max_tokens"] = kwargs.pop("max_tokens")
        params.update(kwargs)

        return client.chat.completions.create(**params)


async def chat(
    alias: str,
    messages: list[dict[str, Any]],
    *,
    tools: list[dict] | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """非流式调用：用于 Plan / Reflect / Updater 等决策类节点。

    Args:
        alias: 模型别名（plan / reflect / answer / updater / fallback）
        messages: OpenAI 格式消息列表
        tools: Function Calling 工具描述（可选）
        temperature / max_tokens: 覆盖 yaml 中默认值（可选）

    Returns:
        OpenAI 格式响应字典，业务方按 ["choices"][0]["message"] 取
    """
    try:
        return await _do_chat(alias, messages, tools=tools,
                              temperature=temperature, max_tokens=max_tokens, **kwargs)
    except Exception as e:
        # fallback 降级
        fallback_aliases = _FALLBACKS.get(alias, [])
        for fb in fallback_aliases:
            try:
                logger.warning(f"[llm] {alias} 失败({e})，降级到 {fb}")
                return await _do_chat(fb, messages, tools=tools,
                                      temperature=temperature, max_tokens=max_tokens, **kwargs)
            except Exception as fe:
                logger.warning(f"[llm] fallback {fb} 也失败: {fe}")
        raise


async def _do_chat(
    alias: str,
    messages: list[dict[str, Any]],
    *,
    tools: list[dict] | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """实际调用（无 fallback 逻辑）。"""
    client, model, default_temp = _get_client(alias)

    params: dict[str, Any] = {"model": model, "messages": messages}
    # temperature 优先级：调用方 > yaml 默认
    temp = temperature if temperature is not None else default_temp
    if temp is not None:
        params["temperature"] = temp
    if max_tokens is not None:
        params["max_tokens"] = max_tokens
    if tools is not None:
        params["tools"] = tools
    params.update(kwargs)

    response = await client.chat.completions.create(**params)
    return response.model_dump()


async def chat_stream(
    alias: str,
    messages: list[dict[str, Any]],
    *,
    temperature: float | None = None,
    **kwargs: Any,
) -> AsyncIterator[str]:
    """流式调用：用于 Answer 节点的 SSE token 推送。

    Yields:
        每次产出一段增量 token（已抽取 delta.content，调用方直接拼）
    """
    client, model, default_temp = _get_client(alias)

    params: dict[str, Any] = {"model": model, "messages": messages, "stream": True}
    temp = temperature if temperature is not None else default_temp
    if temp is not None:
        params["temperature"] = temp
    params.update(kwargs)

    stream = await client.chat.completions.create(**params)
    async for chunk in stream:
        try:
            delta = chunk.choices[0].delta
            content = getattr(delta, "content", None)
            if content:
                yield content
        except (AttributeError, IndexError):
            continue
