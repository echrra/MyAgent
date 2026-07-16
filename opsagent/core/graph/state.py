"""LangGraph 全局 State 定义。

约定:
- 所有节点共享同一份 State（TypedDict），节点函数返回 dict（部分字段）由 LangGraph 自动 merge
- W1 仅启用最小字段集；profile_context / episodic_messages / citations 等在 W2-W4 接入后才填实数据
- `working_memory` 用 Annotated[..., add] 表示"每次累加而非覆盖"，方便多轮工具调用累计
- W6 扩展：Multi-Agent 假设驱动并行诊断，新增 Hypothesis / WorkerResult / 对应 State 字段
"""

from operator import add
from typing import Annotated, Any, Literal, TypedDict

# Plan 节点产出的下一步动作
NextAction = Literal["call_tool", "answer", "stop"]


class ToolCallRecord(TypedDict):
    """单次工具调用的完整记录（也是工作记忆的最小单元）。"""

    tool_name: str
    args: dict[str, Any]
    result: Any  # 可能是 dict / str / list，由工具自定义
    success: bool
    latency_ms: int
    error: str | None


# ---------- W6 Multi-Agent 类型 ----------


class Hypothesis(TypedDict):
    """Coordinator 生成的单个故障假设。"""

    id: str                     # "h1", "h2", "h3"
    description: str            # 假设描述（如"级联超时导致上游阻塞"）
    fault_domain: str           # 故障域提示（如"timeout/cascade"）
    suggested_tools: list[str]  # 建议的工具列表


class WorkerResult(TypedDict):
    """单个 Worker 的诊断结果。"""

    hypothesis_id: str
    hypothesis: str
    evidence: list[dict[str, Any]]       # 关键证据摘要
    confidence: float                     # 0.0-1.0 置信度
    conclusion: str                       # 一句话结论
    tool_records: list[ToolCallRecord]    # 该 Worker 的工具调用记录


class AgentState(TypedDict, total=False):
    """Agent 全局状态。

    total=False 表示所有字段都不强制；节点按需读写。
    """

    # ---------- 会话标识 ----------
    session_id: str
    user_id: str
    trace_id: str  # W5 接 Langfuse 时填

    # ---------- 用户输入 ----------
    user_query: str

    # ---------- 上下文装配（W2-W4 才填实数据；W1 用空值占位）----------
    system_prompt: str
    profile_context: str
    episodic_messages: list[dict[str, Any]]
    working_memory: Annotated[list[ToolCallRecord], add]

    # ---------- 推理状态（v1 ReAct 循环核心，v2 保留兼容）----------
    plan: str | None
    next_action: NextAction | None
    pending_tool_call: dict[str, Any] | None
    iteration: int  # 当前已执行的 Plan-Tool-Reflect 轮次

    # ---------- Multi-Agent 状态（W6）----------
    hypotheses: list[Hypothesis]                              # Coordinator 产出
    worker_results: Annotated[list[WorkerResult], add]        # Worker 产出，add 跨实例累加

    # ---------- 输出 ----------
    final_answer: str | None
    citations: list[dict[str, Any]]

    # ---------- 控制 ----------
    error: str | None


# ---------- 常量 ----------
MAX_ITERATIONS = 5
"""ReAct 循环最大轮数；触顶后 reflect 节点强制走 answer，防死循环。"""
