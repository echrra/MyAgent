"""MCP Server：知识库检索（kb_server）—— 把 W2 混合检索包成 MCP 工具。

独立可跑（stdio 传输）：
    python -m opsagent.core.tools.mcp_servers.kb_server

为什么有它（FC + MCP 双轨）：
项目已有 FC 版 search_sop（直接函数调用）。本 server 把同一套 retrieve() 流水线
（向量+BM25→RRF→rerank）暴露成 MCP 协议工具 kb_search，用于演示「同一能力可经
两种传输被 Agent 调用」。返回结构与 FC 版对齐，便于双轨对比。

返回约定：工具返回 JSON 字符串（非裸 dict）——客户端统一 json.loads(content[0].text)，
跨 mcp 版本最稳，不依赖 structuredContent 的序列化细节。
"""

from __future__ import annotations

import json
from typing import Any

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("opsagent-kb")


def _chunk_snippet(content: str, limit: int = 160) -> str:
    """从 chunk 正文取摘要：去掉 [title] section 前缀，单行展示（对齐 FC 版 search_sop）。"""
    body = content.split("\n\n", 1)[-1] if "\n\n" in content else content
    return " ".join(body.split())[:limit]


@mcp.tool()
def kb_search(query: str, top_k: int = 5) -> str:
    """语义+关键词混合检索知识库（SOP / 复盘 / runbook / 服务说明）。

    Args:
        query: 检索词
        top_k: 返回条数上限

    Returns:
        JSON 字符串：{"data": [{doc_id,title,snippet,score,category,section,source_lines}...],
                      "meta": {query, top_k, mode, recall_vec, recall_bm25, reranked, transport}}
    """
    # 延迟 import：避免没装 torch/未起库时 import 本模块即失败；server 进程内首查触发模型加载
    from opsagent.core.retrieval.pipeline import retrieve

    out = retrieve(query, top_k=top_k)
    data: list[dict[str, Any]] = [
        {
            "doc_id": r["doc_id"],
            "title": r["title"],
            "snippet": _chunk_snippet(r["content"]),
            "score": r["score"],
            "category": r["category"],
            "section": r["section"],
            "source_lines": r["source_lines"],
        }
        for r in out["results"]
    ]
    payload = {
        "data": data,
        "meta": {
            "query": query,
            "top_k": top_k,
            "mode": "hybrid",
            "recall_vec": out["recall_vec"],
            "recall_bm25": out["recall_bm25"],
            "reranked": out["reranked"],
            "transport": "mcp",  # 标记本结果经 MCP 协议返回，便于双轨区分
        },
    }
    return json.dumps(payload, ensure_ascii=False)


if __name__ == "__main__":
    # stdio 传输：由 MCP 客户端以子进程方式拉起并通过标准输入输出通信
    mcp.run()
