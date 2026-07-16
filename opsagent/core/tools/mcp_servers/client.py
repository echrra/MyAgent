"""MCP 客户端桥 —— 把异步 MCP 会话接进同步的 tool_exec 契约。

难点与方案（设计要点）：
tool_exec 通过 `asyncio.to_thread(TOOL_REGISTRY[name], **args)` 同步调工具，而 MCP
客户端是异步的（stdio_client / ClientSession 都是 async 上下文）。直接在主事件循环里
跑会嵌套循环、且会话与「创建它的循环」强绑定。

方案：起一个**守护线程跑独立 asyncio 事件循环**，两个 MCP server 子进程的会话**常驻**
其上（用 AsyncExitStack 进入 async 上下文且不关闭，让底层读写任务在该循环长跑）。
同步侧用 `asyncio.run_coroutine_threadsafe(coro, loop).result(timeout)` 把调用 marshal
到那个循环。阻塞等待发生在 base.Tool 的 worker 线程里，**不阻塞主 SSE 事件循环**。

懒启动 + 优雅降级：进程 import 不拉子进程；首次调用才启动。某个 server 起不来只标记
不可用，调用其工具时抛错（base.Tool 记 success=False，Agent 可走替代路径），不整体崩。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
from contextlib import AsyncExitStack
from typing import Any

from loguru import logger
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from opsagent.core.config import PROJECT_ROOT, settings

# server 名 → 启动该 server 的模块路径（以子进程 python -m 方式拉起）
_SERVER_MODULES: dict[str, str] = {
    "kb": "opsagent.core.tools.mcp_servers.kb_server",
    "metrics": "opsagent.core.tools.mcp_servers.metrics_server",
}


class _MCPManager:
    """常驻的 MCP 客户端管理器：后台循环 + 多 server 会话。"""

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._stack: AsyncExitStack | None = None
        self._sessions: dict[str, ClientSession] = {}
        self._started = False
        self._lock = threading.Lock()  # 保护懒启动，避免并发首调重复拉起

    # ---------- 启动 ----------

    def _ensure_started(self) -> None:
        """懒启动：起后台循环线程，并在其上建立两个 server 会话（仅一次）。"""
        if self._started:
            return
        with self._lock:
            if self._started:
                return
            self._loop = asyncio.new_event_loop()
            self._thread = threading.Thread(
                target=self._loop.run_forever, name="mcp-loop", daemon=True
            )
            self._thread.start()
            # 在后台循环上同步等待会话建立完成
            fut = asyncio.run_coroutine_threadsafe(self._startup(), self._loop)
            fut.result(timeout=settings.mcp_call_timeout_s)
            self._started = True
            logger.info(f"[mcp] 客户端就绪，可用 server: {sorted(self._sessions)}")

    async def _startup(self) -> None:
        """在后台循环里逐个拉起 server 子进程并建会话，会话常驻（不关闭 stack）。"""
        self._stack = AsyncExitStack()
        for name, module in _SERVER_MODULES.items():
            try:
                params = StdioServerParameters(
                    command=sys.executable,
                    args=["-m", module],
                    cwd=str(PROJECT_ROOT),
                    env=dict(os.environ),
                )
                read, write = await self._stack.enter_async_context(stdio_client(params))
                session = await self._stack.enter_async_context(ClientSession(read, write))
                await session.initialize()
                self._sessions[name] = session
                logger.info(f"[mcp] server '{name}' 会话已建立（{module}）")
            except Exception as exc:  # 单个 server 起不来不影响其它，优雅降级
                logger.warning(f"[mcp] server '{name}' 启动失败，标记不可用: {exc}")

    # ---------- 调用 ----------

    def call(
        self, server: str, tool: str, args: dict[str, Any], timeout: float | None = None
    ) -> dict[str, Any]:
        """同步调用某 server 的某工具，返回解析后的 dict（{data, meta}）。

        会话不可用或工具报错时抛异常，交给 base.Tool 记 success=False。
        """
        self._ensure_started()
        if self._loop is None:
            raise RuntimeError("mcp 后台循环未启动")
        if server not in self._sessions:
            raise RuntimeError(f"mcp server '{server}' 不可用（启动失败或未配置）")

        timeout = timeout if timeout is not None else settings.mcp_call_timeout_s
        fut = asyncio.run_coroutine_threadsafe(
            self._call(server, tool, args), self._loop
        )
        return fut.result(timeout=timeout)

    async def _call(self, server: str, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        session = self._sessions[server]
        result = await session.call_tool(tool, args)
        if result.isError:
            raise RuntimeError(f"mcp 工具 {server}.{tool} 返回错误: {self._extract_text(result)}")
        return self._parse(result)

    # ---------- 结果解析 ----------

    @staticmethod
    def _extract_text(result: Any) -> str:
        """从 CallToolResult 取第一段文本内容。"""
        for item in getattr(result, "content", []) or []:
            text = getattr(item, "text", None)
            if text is not None:
                return text
        return ""

    def _parse(self, result: Any) -> dict[str, Any]:
        """server 约定返回 JSON 字符串 → 解析回 dict。"""
        text = self._extract_text(result)
        if not text:
            return {"data": None, "meta": {"transport": "mcp", "warning": "空结果"}}
        return json.loads(text)


# 进程级单例（与 get_graph / embedder 等懒加载风格一致）
_manager: _MCPManager | None = None
_manager_lock = threading.Lock()


def get_mcp_manager() -> _MCPManager:
    """获取 MCP 客户端管理器单例（首次调用时才真正拉起 server 子进程）。"""
    global _manager
    if _manager is None:
        with _manager_lock:
            if _manager is None:
                _manager = _MCPManager()
    return _manager
