"""SSE 聊天端点 —— 跑 Agent 图并把过程流式推给前端。

事件协议（event: 类型 + data: JSON）：
- thinking   : plan/coordinator 节点决策  {thought, next_action, iteration} 或 {hypotheses}
- tool       : 每次工具调用结果           {tool_name, args, success, latency_ms, summary}
- hypothesis : Worker 假设诊断结论        {hypothesis_id, hypothesis, confidence, conclusion}
- token      : answer/synthesizer 流式增量 {"t": "<增量文本>"}
- done       : 图执行结束                 {iteration, tool_calls, final_len, session_id}
- error      : 异常兜底                   {message}

流式原理：
- graph.astream(stream_mode=["custom","updates"]) 同时产出两类流：
  * custom  ← answer/synthesizer 节点通过 get_stream_writer 推的 token
  * updates ← 每个节点返回的部分 state（含 plan thought / tool 记录 / hypothesis 结果）
"""

import json
import uuid
from collections.abc import AsyncIterator

from fastapi import APIRouter
from loguru import logger
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from opsagent.core.graph.builder import get_graph
from opsagent.core.graph.state import AgentState
from opsagent.core.memory import compact_session

router = APIRouter()


class ChatRequest(BaseModel):
    """对话请求体。session_id 为空时后端生成并在 done 事件回传。"""

    query: str
    session_id: str | None = None
    user_id: str = "anonymous"


class CompactRequest(BaseModel):
    """手动压缩请求体。"""

    session_id: str


def _summarize_tool_result(tool_name: str, result: object) -> str:
    """把工具结果压成一句话摘要，避免把整个 data 塞进 SSE。"""
    if not isinstance(result, dict):
        return str(result)[:120]
    meta = result.get("meta", {})
    if tool_name == "search_logs":
        return f"命中 {meta.get('hit', 0)} 条日志（返回 {meta.get('returned', 0)} 条）"
    if tool_name == "search_sop":
        return f"命中 {meta.get('n_hit', 0)} 篇文档（返回 top{meta.get('top_k', 0)}）"
    if tool_name == "get_service_metrics":
        data = result.get("data", {})
        return f"错误率 {data.get('error_rate')} / P99 {data.get('p99_latency_ms')}ms"
    if tool_name == "trace_query":
        if meta.get("mode") == "by_trace":
            data = result.get("data", {})
            summ = data.get("summary", {}) if isinstance(data, dict) else {}
            return (
                f"链路 {meta.get('found', 0)} 跨度节点，"
                f"错误 {summ.get('error_count', 0)} / 最慢 {summ.get('max_latency_ms', 0)}ms"
            )
        return f"命中 {meta.get('hit', 0)} 条链路（返回 {meta.get('returned', 0)} 条）"
    if tool_name == "change_query":
        return f"近期变更 {meta.get('returned', 0)} 条"
    if tool_name == "create_ticket":
        data = result.get("data", {})
        return f"已创建工单 {data.get('ticket_id')}（{data.get('severity')}）"
    if tool_name == "kb_search":
        data = result.get("data", [])
        n = len(data) if isinstance(data, list) else 0
        return f"[MCP] 命中 {n} 篇文档（top{meta.get('top_k', 0)}）"
    if tool_name == "query_metrics":
        data = result.get("data", {})
        return f"[MCP] 指标 {meta.get('metric')} → {data}"
    return json.dumps(meta, ensure_ascii=False)[:120]


def _sse(event: str, payload: dict) -> dict:
    """构造 sse-starlette 事件字典。"""
    return {"event": event, "data": json.dumps(payload, ensure_ascii=False)}


async def _event_stream(req: ChatRequest) -> AsyncIterator[dict]:
    """跑图并把过程翻译成 SSE 事件序列。"""
    session_id = req.session_id or f"sess-{uuid.uuid4().hex[:12]}"
    graph = get_graph()
    initial: AgentState = {
        "session_id": session_id,
        "user_id": req.user_id,
        "user_query": req.query,
    }

    iteration = 0
    tool_calls = 0
    final_len = 0

    try:
        async for mode, payload in graph.astream(
            initial, stream_mode=["custom", "updates"]
        ):
            if mode == "custom":
                # answer 节点推的 token
                if isinstance(payload, dict) and payload.get("type") == "token":
                    tok = payload.get("data", "")
                    final_len += len(tok)
                    yield _sse("token", {"t": tok})
                continue

            # mode == "updates"：payload = {节点名: 该节点返回的部分 state}
            for node_name, upd in payload.items():
                if not isinstance(upd, dict):
                    continue
                # v1: plan 节点
                if node_name == "plan_node":
                    iteration = upd.get("iteration", iteration)
                    if upd.get("plan"):
                        yield _sse("thinking", {
                            "thought": upd.get("plan", ""),
                            "next_action": upd.get("next_action"),
                            "iteration": iteration,
                        })
                # v1: tool_exec 节点
                elif node_name == "tool_exec" and upd.get("working_memory"):
                    rec = upd["working_memory"][-1]  # 本次追加的记录
                    tool_calls += 1
                    iteration = upd.get("iteration", iteration)
                    yield _sse("tool", {
                        "tool_name": rec["tool_name"],
                        "args": rec["args"],
                        "success": rec["success"],
                        "latency_ms": rec["latency_ms"],
                        "summary": _summarize_tool_result(rec["tool_name"], rec["result"]),
                    })
                # v2: coordinator 节点
                elif node_name == "coordinator" and upd.get("hypotheses"):
                    hypotheses = upd["hypotheses"]
                    yield _sse("thinking", {
                        "thought": f"生成 {len(hypotheses)} 个故障假设",
                        "hypotheses": [h.get("description", "") for h in hypotheses],
                        "next_action": "investigate",
                    })
                # v2: worker 节点（工具调用 + 假设结论）
                elif node_name == "worker":
                    for rec in upd.get("working_memory", []):
                        tool_calls += 1
                        yield _sse("tool", {
                            "tool_name": rec["tool_name"],
                            "args": rec["args"],
                            "success": rec["success"],
                            "latency_ms": rec["latency_ms"],
                            "summary": _summarize_tool_result(rec["tool_name"], rec["result"]),
                        })
                    for wr in upd.get("worker_results", []):
                        yield _sse("hypothesis", {
                            "hypothesis_id": wr.get("hypothesis_id"),
                            "hypothesis": wr.get("hypothesis"),
                            "confidence": wr.get("confidence"),
                            "conclusion": wr.get("conclusion"),
                        })

        yield _sse("done", {
            "iteration": iteration,
            "tool_calls": tool_calls,
            "final_len": final_len,
            "session_id": session_id,
        })
    except Exception as exc:
        logger.exception("[/chat] 流式执行异常")
        yield _sse("error", {"message": f"{type(exc).__name__}: {exc}"})


@router.post("/chat")
async def chat_sse(req: ChatRequest) -> EventSourceResponse:
    """POST /chat —— 返回 SSE 流（thinking → tool → token → done）。"""
    logger.info(f"[/chat] query={req.query!r} session={req.session_id}")
    # ping=15：每 15s 发心跳，防代理/网关掐断长连接
    return EventSourceResponse(_event_stream(req), ping=15)


@router.post("/compact")
async def compact_endpoint(req: CompactRequest) -> dict:
    """POST /compact —— 手动触发指定会话的对话压缩（docs/04 的「手动 /compact」）。

    同步等待压缩完成并回传摘要（与自动触发的 fire-and-forget 不同：手动是用户显式发起，
    希望立即看到结果）。压缩失败时 compacted=false、summary=null（原文不丢）。
    """
    logger.info(f"[/compact] session={req.session_id}")
    summary = await compact_session(req.session_id, reason="manual")
    return {
        "session_id": req.session_id,
        "compacted": summary is not None,
        "summary": summary,
    }
