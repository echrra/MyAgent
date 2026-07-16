"""工具聚合层 —— 给 Plan/tool_exec 节点提供统一的工具注册表与说明。

把真实工具（real.py）和仍为 mock 的工具（mock.py）合并成单一入口：
- TOOL_REGISTRY    : {工具名: Tool 实例}，tool_exec 按名分发
- TOOL_DESCRIPTIONS: 拼进 Plan prompt 的工具说明文本

W3：每个工具不再是裸函数，而是包进 base.Tool 统一治理（入参校验 / 超时 / 重试 /
输出截断 / trace 打点）。Tool 实例对外仍是 `(**kwargs) -> {data, meta}` 的同步可调用，
tool_exec / nodes.py 无需改动。

未来某个 mock 工具接入真实数据时，只需把它从 mock 挪到 real 并改这里的引用，
节点代码无需改动。
"""

from pydantic import BaseModel, Field, model_validator

from opsagent.core.tools.base import Tool
from opsagent.core.tools.mcp_servers.client import get_mcp_manager
from opsagent.core.tools.mock import change_query, create_ticket, get_service_metrics
from opsagent.core.tools.real import search_logs, search_sop, trace_query


# ---------- 入参模型（Pydantic 校验 LLM 给的工具参数）----------
class SearchLogsArgs(BaseModel):
    """search_logs 入参。"""

    service: str = Field(description="服务名")
    keyword: str = Field(default="", description="关键词，空则只按服务过滤")
    minutes: int = Field(default=10, ge=1, description="时间窗（分钟）")
    at: str = Field(default="", description="锚点时间 ISO8601（如 2026-06-17T13:49:53Z）；给了则以 at 为中心前后 minutes/2 查云端；不给则默认从现在往前推")


class SearchSopArgs(BaseModel):
    """search_sop 入参。"""

    query: str = Field(description="检索词")
    top_k: int = Field(default=3, ge=1, le=20, description="返回条数上限")


class GetServiceMetricsArgs(BaseModel):
    """get_service_metrics 入参。"""

    service: str = Field(description="服务名")
    minutes: int = Field(default=5, ge=1, description="时间窗（分钟）")


class TraceQueryArgs(BaseModel):
    """trace_query 入参：trace_id / service 至少给一个。"""

    trace_id: str = Field(default="", description="链路 ID，给定则下钻该链路时间线")
    service: str = Field(default="", description="服务名，仅给 service 时列该服务近期可疑链路")
    minutes: int = Field(default=30, ge=1, description="时间窗（分钟）")
    at: str = Field(default="", description="锚点时间 ISO8601；用于查历史故障（例如用户报 2026-06-17 13:49 的 trace）")

    @model_validator(mode="after")
    def _at_least_one(self) -> "TraceQueryArgs":
        # 两者皆空无法定位链路，校验失败（base.Tool 会转成 ToolValidationError，不重试）
        if not self.trace_id.strip() and not self.service.strip():
            raise ValueError("trace_id 与 service 至少提供一个")
        return self


class CreateTicketArgs(BaseModel):
    """create_ticket 入参。"""

    service: str = Field(description="关联服务名")
    title: str = Field(description="工单标题（一句话问题概述）")
    severity: str = Field(default="P2", description="严重级别 P0/P1/P2/P3")
    description: str = Field(default="", description="详细描述（可选）")


class ChangeQueryArgs(BaseModel):
    """change_query 入参。"""

    service: str = Field(default="", description="服务名，空则返回全部服务的变更")
    minutes: int = Field(default=60, ge=1, description="时间窗（分钟）")


# ---------- MCP 工具入参模型（与对应 server 暴露的工具签名一致）----------
class KbSearchArgs(BaseModel):
    """kb_search（MCP）入参。"""

    query: str = Field(description="检索词")
    top_k: int = Field(default=3, ge=1, le=20, description="返回条数上限")


class QueryMetricsArgs(BaseModel):
    """query_metrics（MCP）入参。"""

    service: str = Field(description="服务名")
    metric: str = Field(default="all", description="指标维度 all/error_rate/p99/qps")
    minutes: int = Field(default=5, ge=1, description="时间窗（分钟）")


# ---------- MCP 工具的同步包装（经客户端桥跨进程调用对应 server）----------
def _mcp_kb_search(**kwargs: object) -> dict:
    """经 MCP 协议调用 kb server 的 kb_search（入参已由 Tool 层校验）。"""
    return get_mcp_manager().call("kb", "kb_search", dict(kwargs))


def _mcp_query_metrics(**kwargs: object) -> dict:
    """经 MCP 协议调用 metrics server 的 query_metrics（入参已由 Tool 层校验）。"""
    return get_mcp_manager().call("metrics", "query_metrics", dict(kwargs))


# ---------- 注册表（tool_exec 按名查表分发，值为 Tool 实例）----------
# search_sop 首查要加载 embedder+reranker（CPU 冷启 ~10-30s），单独放宽超时到 60s，
# 避免默认 10s 把模型加载误判为超时、连带其内部关键词降级都来不及触发。
# kb_search 同理：模型加载发生在 MCP server 子进程内，首查同样需 60s。
TOOL_REGISTRY: dict[str, Tool] = {
    "search_logs": Tool(
        name="search_logs",
        fn=search_logs,
        args_model=SearchLogsArgs,
        description="按服务名+关键词查合成日志",
    ),
    "search_sop": Tool(
        name="search_sop",
        fn=search_sop,
        args_model=SearchSopArgs,
        description="语义+关键词混合检索知识库",
        timeout_s=60.0,
        max_retries=0,  # 重试只会再占 60s 检索池槽、加剧争抢，CPU 任务不值得重试
        heavy=True,     # 走独立检索池，避免饿死轻量工具
    ),
    "get_service_metrics": Tool(
        name="get_service_metrics",
        fn=get_service_metrics,
        args_model=GetServiceMetricsArgs,
        description="查某服务最近 N 分钟的 QPS/错误率/延迟",
    ),
    "trace_query": Tool(
        name="trace_query",
        fn=trace_query,
        args_model=TraceQueryArgs,
        description="查调用链路：给 trace_id 看时间线，给 service 列近期可疑链路",
    ),
    "create_ticket": Tool(
        name="create_ticket",
        fn=create_ticket,
        args_model=CreateTicketArgs,
        description="创建运维工单，返回工单号",
    ),
    "change_query": Tool(
        name="change_query",
        fn=change_query,
        args_model=ChangeQueryArgs,
        description="查某服务近期变更（发布/配置/扩缩容）",
    ),
    # ---- 以下为 MCP 协议工具（与上面 FC 工具同台，演示双轨）----
    "kb_search": Tool(
        name="kb_search",
        fn=_mcp_kb_search,
        args_model=KbSearchArgs,
        description="（MCP）语义+关键词混合检索知识库",
        timeout_s=60.0,
        max_retries=0,
        heavy=True,
    ),
    "query_metrics": Tool(
        name="query_metrics",
        fn=_mcp_query_metrics,
        args_model=QueryMetricsArgs,
        description="（MCP）查某服务 RED 指标，可按 metric 维度过滤",
    ),
}


# ---------- 给 Plan 节点看的工具说明（拼进 prompt）----------
# 标注真实服务名，引导 Plan 用实际存在的服务，避免编造不存在的服务名。
TOOL_DESCRIPTIONS = """\
- search_logs(service: str, keyword: str = "", minutes: int = 10, at: str = ""): 按服务名+关键词查日志。默认查本地评测集；若线上诊断且开了 TLS fallback，会自动查云端。
  可用服务名：edgectl-backend-http（核心 HTTP API）/ edgectl-admin（后台管控）/ edgectl-backend-watcher（事件监听+定时任务）/ edgectl-backend-scheduler（调度器）。
  at 参数：ISO8601 时间锚点。如果用户在 query 里给了具体时间（如 "2026-06-17 13:49:53"），必须把它转成 "2026-06-17T13:49:53" 传给 at；未带时区时按北京时间理解。
- search_sop(query: str, top_k: int = 3): 语义+关键词混合检索知识库（SOP 排查指南 / 故障复盘 / 中间件手册 / 服务说明），向量召回+BM25+RRF+rerank。
- get_service_metrics(service: str, minutes: int = 5): 查某服务最近 N 分钟的 QPS/错误率/延迟。
- trace_query(trace_id: str = "", service: str = "", minutes: int = 30, at: str = ""): 查调用链路（trace_id/service 至少给一个）。给 trace_id 时优先精确查——若本地无命中且开了 TLS fallback，自动查云端。at 参数用法同 search_logs：用户给了绝对时间就传 at。给 service 先列近期可疑链路挑一个 trace_id 再下钻。
- change_query(service: str = "", minutes: int = 60): 查某服务近期变更（发布/配置/扩缩容）。排障常用——「发版即出事」是高频根因。
- create_ticket(service: str, title: str, severity: str = "P2", description: str = ""): 定位到根因后创建运维工单，返回工单号。
- kb_search(query: str, top_k: int = 3): 【MCP 协议】混合检索知识库，能力同 search_sop，经 MCP server 调用（双轨之一）。
- query_metrics(service: str, metric: str = "all", minutes: int = 5): 【MCP 协议】查服务 RED 指标，可按 metric 维度过滤（all/error_rate/p99/qps）。
"""

__all__ = ["TOOL_DESCRIPTIONS", "TOOL_REGISTRY"]
