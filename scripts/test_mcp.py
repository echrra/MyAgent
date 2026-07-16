"""MCP 双轨烟测：以纯 MCP 客户端独立拉起两个 server，验证「独立可跑 + 协议跑通」。

跑法:
    make test-mcp
    # 或
    uv run python scripts/test_mcp.py

验证点（对应 W3 验收「MCP Server 能独立启动，Agent 通过 MCP 协议调用成功」）:
- kb_server / metrics_server 都能以 `python -m ...` 子进程方式独立拉起
- 客户端 initialize + list_tools 能拿到工具清单
- call_tool 各调一次有结构化返回

注意：本脚本不走 Agent，也不用项目内的客户端桥（client.py），而是直接用 MCP SDK 的
stdio_client，独立证明 server 与协议本身可用，与「桥接进 Agent」解耦。
"""

import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# (server 显示名, 模块路径, 探测用的 (工具名, 入参))
_SERVERS = [
    (
        "kb",
        "opsagent.core.tools.mcp_servers.kb_server",
        ("kb_search", {"query": "DNS 解析失败 no such host", "top_k": 2}),
    ),
    (
        "metrics",
        "opsagent.core.tools.mcp_servers.metrics_server",
        ("query_metrics", {"service": "edgectl-backend-http", "metric": "error_rate"}),
    ),
]


def _extract_text(result) -> str:
    for item in getattr(result, "content", []) or []:
        text = getattr(item, "text", None)
        if text is not None:
            return text
    return ""


async def _probe_one(name: str, module: str, probe: tuple) -> bool:
    tool_name, args = probe
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", module],
        cwd=str(PROJECT_ROOT),
        env=dict(os.environ),
    )
    print(f"\n=== server '{name}'（{module}）===")
    async with (
        stdio_client(params) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        tools = await session.list_tools()
        names = [t.name for t in tools.tools]
        print(f"  list_tools → {names}")
        if tool_name not in names:
            print(f"  ❌ 期望工具 {tool_name} 未暴露")
            return False
        print(f"  call_tool {tool_name}({args}) ...")
        result = await session.call_tool(tool_name, args)
        if result.isError:
            print(f"  ❌ 工具返回错误: {_extract_text(result)}")
            return False
        parsed = json.loads(_extract_text(result))
        data = parsed.get("data")
        meta = parsed.get("meta", {})
        n = len(data) if isinstance(data, list) else ("dict" if data else 0)
        print(f"  ✓ 返回 data={n} 条/项, meta.transport={meta.get('transport')}")
        return True


async def _main() -> int:
    results = []
    for name, module, probe in _SERVERS:
        try:
            ok = await _probe_one(name, module, probe)
        except Exception as exc:  # 子进程起不来 / 协议异常
            print(f"  ❌ server '{name}' 烟测异常: {type(exc).__name__}: {exc}")
            ok = False
        results.append(ok)

    print("\n" + "=" * 60)
    if all(results):
        print("🎉 两个 MCP server 独立可跑、协议跑通。")
        return 0
    print("❌ 有 MCP server 烟测未通过。")
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
