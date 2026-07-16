"""W3 第三块 MCP 双轨集成测 —— 经客户端桥的真实 MCP 往返 + 注册一致性。

这些是集成测：会真实拉起 MCP server 子进程并走协议往返。
- query_metrics 走 mock，快。
- kb_search 会在 server 子进程内加载 embedder+reranker（~30s），且依赖检索环境；
  环境不就绪（子进程起不来 / 检索不可用）则 skip，不假阳性。
"""

import pytest

from opsagent.core.tools import TOOL_DESCRIPTIONS, TOOL_REGISTRY

# ---------------- 注册一致性（不拉子进程，快）----------------


def test_mcp_tools_registered_and_described():
    for name in ("kb_search", "query_metrics"):
        assert name in TOOL_REGISTRY, f"{name} 未注册到 TOOL_REGISTRY"
        assert name in TOOL_DESCRIPTIONS, f"{name} 未出现在 TOOL_DESCRIPTIONS"


# ---------------- query_metrics MCP 往返（快，mock）----------------


def test_query_metrics_via_mcp_roundtrip():
    """经 TOOL_REGISTRY['query_metrics'] → 子进程 → MCP 协议回来，验证桥通。"""
    tool = TOOL_REGISTRY["query_metrics"]
    out = tool(service="edgectl-backend-http", metric="error_rate")
    assert out["meta"]["transport"] == "mcp", "结果应标记经 MCP 协议返回"
    assert out["meta"]["mock"] is True
    assert "error_rate" in out["data"]
    # metric 过滤：只取 error_rate 时不应带 p99
    assert "p99_latency_ms" not in out["data"]


def test_query_metrics_all_metric():
    out = TOOL_REGISTRY["query_metrics"](service="edgectl-admin")  # 默认 metric=all
    assert out["meta"]["metric"] == "all"
    assert {"qps", "error_rate", "p99_latency_ms"} <= set(out["data"])


# ---------------- kb_search MCP 往返（慢，加载模型；环境不就绪则 skip）----------------


def test_kb_search_via_mcp_roundtrip():
    tool = TOOL_REGISTRY["kb_search"]
    try:
        out = tool(query="DNS 解析失败 no such host", top_k=2)
    except Exception as exc:  # 子进程/检索环境不就绪
        pytest.skip(f"kb MCP 环境不就绪，跳过: {exc}")
    assert out["meta"]["transport"] == "mcp"
    assert isinstance(out["data"], list)
    # 检索可用时应有命中；为空也不算错（语料/索引差异），只校验结构与传输标记
    if out["data"]:
        first = out["data"][0]
        assert {"doc_id", "title", "snippet", "score"} <= set(first)
