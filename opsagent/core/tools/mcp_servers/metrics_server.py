"""MCP Server：服务指标查询（metrics_server）—— RED 指标查询包成 MCP 工具。

独立可跑（stdio 传输）：
    python -m opsagent.core.tools.mcp_servers.metrics_server

指标时序数据（§2.5 trace/metric_gen）尚未合成 → 本 server 是**诚实 mock**：返回固定的
「明显异常」样本，但比 FC 版 get_service_metrics 多一层 metric 维度过滤
（all / error_rate / p99 / qps），用于演示 MCP 工具的参数能力。

返回约定同 kb_server：JSON 字符串，客户端 json.loads(content[0].text)。
"""

from __future__ import annotations

import json

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("opsagent-metrics")

# 固定的「异常」样本（与 mock.get_service_metrics 同源，集中在此便于讲述 mock 边界）
_SAMPLE = {
    "qps": 132.4,
    "error_rate": 0.18,  # 18%，明显异常
    "p99_latency_ms": 4820,
    "p50_latency_ms": 156,
}


@mcp.tool()
def query_metrics(service: str, metric: str = "all", minutes: int = 5) -> str:
    """查某服务最近 N 分钟的 RED 指标，可按 metric 维度过滤。

    Args:
        service: 服务名
        metric: 指标维度，all / error_rate / p99 / qps；非法值回退 all
        minutes: 时间窗（分钟）

    Returns:
        JSON 字符串：{"data": {...过滤后的指标...}, "meta": {service, metric, minutes, mock, transport}}
    """
    metric = (metric or "all").strip().lower()
    if metric == "error_rate":
        data = {"error_rate": _SAMPLE["error_rate"]}
    elif metric == "p99":
        data = {"p99_latency_ms": _SAMPLE["p99_latency_ms"]}
    elif metric == "qps":
        data = {"qps": _SAMPLE["qps"]}
    else:
        metric = "all"
        data = dict(_SAMPLE)

    payload = {
        "data": data,
        "meta": {
            "service": service,
            "metric": metric,
            "minutes": minutes,
            "mock": True,
            "transport": "mcp",
        },
    }
    return json.dumps(payload, ensure_ascii=False)


if __name__ == "__main__":
    mcp.run()
