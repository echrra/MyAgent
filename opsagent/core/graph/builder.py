"""LangGraph 图编排 —— v1 ReAct 循环 / v2 Multi-Agent 假设驱动并行诊断。

v1 拓扑（ReAct 单 Agent）:
    START → load_memory → plan → tool_exec → reflect → plan... → answer → persist_memory → END

v2 拓扑（W6 Multi-Agent）:
    START → load_memory → coordinator → [worker ×N 并行] → synthesizer → persist_memory → END
    使用 LangGraph Send API 实现 fan-out（coordinator → N 个 worker 并行）→ barrier → synthesizer

设计:
- 节点函数都在 nodes.py，本文件只负责"边"（路由）
- 路由函数纯逻辑，不调 LLM
- graph_version 配置切换 v1/v2（默认 v2）
- 模块级懒加载 + lru_cache，避免每次请求重建图
"""

from functools import lru_cache
from typing import Literal

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send
from loguru import logger

from opsagent.core.config import settings
from opsagent.core.graph import nodes
from opsagent.core.graph.state import AgentState

# ====================== v1 路由函数（ReAct） ======================


def _route_after_plan(state: AgentState) -> Literal["tool_exec", "answer_node", "stop"]:
    """plan_node 节点之后的分支。"""
    action = state.get("next_action")
    if action == "call_tool":
        return "tool_exec"
    if action == "stop":
        return "stop"  # 跳过 tool/reflect/answer，直奔 persist
    return "answer_node"  # 默认走 answer


def _route_after_reflect(state: AgentState) -> Literal["plan_node", "answer_node"]:
    """reflect 节点之后的分支：信息够 → answer；不够 → 回 plan 继续循环。"""
    return "answer_node" if state.get("next_action") == "answer" else "plan_node"


# ====================== v2 路由函数（Multi-Agent） ======================


def _route_to_workers(state: AgentState) -> list[Send]:
    """Coordinator 产出的假设 → 每个假设一个 Send → worker 节点并行执行。"""
    hypotheses = state.get("hypotheses", [])
    if not hypotheses:
        return [Send("synthesizer", {})]

    sends = []
    for h in hypotheses:
        # Send 把当前 state + current_hypothesis 传给 worker
        worker_state = {
            "user_query": state.get("user_query", ""),
            "system_prompt": state.get("system_prompt", ""),
            "profile_context": state.get("profile_context", ""),
            "episodic_messages": state.get("episodic_messages", []),
            "trace_id": state.get("trace_id", ""),
            "current_hypothesis": h,
        }
        sends.append(Send("worker", worker_state))
    return sends


# ====================== 图构造 ======================


def _build_graph_v1():
    """v1：Plan-Tool-Reflect 循环（原 ReAct 单 Agent）。"""
    g = StateGraph(AgentState)

    # 注册节点（节点名加 _node 后缀，避免和 State 字段名 plan/answer 冲突）
    g.add_node("load_memory", nodes.load_memory)
    g.add_node("plan_node", nodes.plan)
    g.add_node("tool_exec", nodes.tool_exec)
    g.add_node("reflect", nodes.reflect)
    g.add_node("answer_node", nodes.answer)
    g.add_node("persist_memory", nodes.persist_memory)

    # 入口
    g.add_edge(START, "load_memory")
    g.add_edge("load_memory", "plan_node")

    # plan 后分支
    g.add_conditional_edges(
        "plan_node",
        _route_after_plan,
        {
            "tool_exec": "tool_exec",
            "answer_node": "answer_node",
            "stop": "persist_memory",
        },
    )

    # tool_exec → reflect（永远）
    g.add_edge("tool_exec", "reflect")

    # reflect 后分支
    g.add_conditional_edges(
        "reflect",
        _route_after_reflect,
        {
            "plan_node": "plan_node",
            "answer_node": "answer_node",
        },
    )

    # 收尾
    g.add_edge("answer_node", "persist_memory")
    g.add_edge("persist_memory", END)

    return g.compile()


def _build_graph_v2():
    """v2：Multi-Agent 假设驱动并行诊断（W6）。

    coordinator → Send fan-out → N 个 worker 并行 → barrier → synthesizer。
    """
    g = StateGraph(AgentState)

    g.add_node("load_memory", nodes.load_memory)
    g.add_node("coordinator", nodes.coordinator)
    g.add_node("worker", nodes.worker)
    g.add_node("synthesizer", nodes.synthesizer)
    g.add_node("persist_memory", nodes.persist_memory)

    # 入口
    g.add_edge(START, "load_memory")
    g.add_edge("load_memory", "coordinator")

    # coordinator → fan-out to workers（Send API）
    g.add_conditional_edges("coordinator", _route_to_workers)

    # worker → synthesizer（所有 worker 完成后自动汇合）
    g.add_edge("worker", "synthesizer")

    # 收尾
    g.add_edge("synthesizer", "persist_memory")
    g.add_edge("persist_memory", END)

    return g.compile()


@lru_cache(maxsize=1)
def get_graph():
    """模块级单例。按 graph_version 配置切换 v1/v2。

    构建完成后启动后台线程预热 embedder + reranker（BGE 系列冷加载 3-8s），
    让首个真实请求命中 search_sop 时模型已就绪，避免 40s+ 首响延迟。
    """
    graph = _build_graph_v1() if settings.graph_version == "v1" else _build_graph_v2()
    _spawn_model_warmup()
    return graph


def _spawn_model_warmup() -> None:
    """后台预热 embedder + reranker，daemon 线程，不阻塞 get_graph 返回。

    首次请求命中 search_sop 时，如果预热已完成 → 直接用；如果还在加载中 →
    在 embedder/reranker 的 double-checked lock 内串行等待，仍然只加载一次。
    """
    import threading

    def _preload() -> None:
        try:
            from opsagent.core.retrieval.embedder import get_embedder
            from opsagent.core.retrieval.reranker import get_reranker
            get_embedder()
            get_reranker()
            logger.info("[warmup] embedder + reranker 已预热")
        except Exception as exc:  # noqa: BLE001
            # 预热失败不致命 —— 首次真实请求会走同样加载路径重试；
            # 只是首响会退化到"冷启动 + 3-5s 加载"，不影响正确性。
            logger.warning(f"[warmup] 预热失败（首响会冷启动，不影响正确性）: {exc}")

    threading.Thread(target=_preload, daemon=True, name="opsagent-model-warmup").start()
