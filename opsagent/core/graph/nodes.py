"""LangGraph 节点函数集合。

v1 节点（ReAct 循环）:
- load_memory  : 装配上下文（W4-A 动态 System；W4-B 接通 L3 Episodic 多轮）
- plan         : LLM 单步决策（call_tool / answer / stop）
- tool_exec    : 执行 pending_tool_call，结果写 working_memory
- reflect      : 判断信息是否足够（够 → answer；不够 → 回 plan）
- answer       : 生成最终答案
- persist_memory: 落库本轮对话（W4-B 写 episodic_turns；W4-C 接压缩触发）

v2 节点（W6 Multi-Agent 假设驱动并行诊断）:
- coordinator  : 生成故障假设列表
- worker       : 并行验证单个假设（内部 plan→exec→judge）
- synthesizer  : 汇总证据生成最终答案（流式）

设计要点:
- 节点都是 async，避免阻塞事件循环
- 节点只返回 dict（部分字段），由 LangGraph merge 进 State
- 节点内严禁直接调 os.environ / openai.ChatCompletion，统一走 opsagent.core.llm.chat
"""

import asyncio
import json
import re
import time
from typing import Any

from langgraph.config import get_stream_writer
from loguru import logger

from opsagent.core.config import settings
from opsagent.core.graph.state import (
    MAX_ITERATIONS,
    AgentState,
    ToolCallRecord,
    WorkerResult,
)
from opsagent.core.llm.client import chat, chat_stream
from opsagent.core.memory import (
    build_system_prompt,
    compact_session,
    compact_working_memory,
    count_tokens,
    load_episodic,
    load_profile,
    persist_turn,
    render_history,
    resolve_max_context,
    run_profile_updater,
    should_compact,
)
from opsagent.core.observability import span_context, update_span
from opsagent.core.prompts import load as load_prompt
from opsagent.core.tools import TOOL_DESCRIPTIONS, TOOL_REGISTRY, tls_client

# ====================== 内部工具 ======================

# 背景压缩任务的强引用集合：create_task 的返回值若不持有，可能被 GC 提前回收导致任务夭折。
# 完成后用 done_callback 自动移除。（W4 边界：仍是 fire-and-forget，正式应换后台任务队列。）
_BG_TASKS: set[asyncio.Task] = set()


def _format_tool_history(working_memory: list[ToolCallRecord] | None) -> str:
    """把工作记忆格式化成 prompt 里好读的文本。"""
    if not working_memory:
        return "（暂无工具调用记录）"
    # 日志截断上限按数据源分层（IO 层的部分分离）：
    #   真实环境(开 TLS fallback)：日志体量大、要读原文定位根因 → trace_query 8000 / search_logs 5000；
    #   合成评测：走精简上限 400，避免大 IO 稀释上下文、把 Agent 推离 search_sop 而拖低 cite。
    # 推理逻辑保持同一套，仅"喂多少字"随数据源变。
    heavy_logs = tls_client.is_enabled()
    lines = []
    for idx, rec in enumerate(working_memory, 1):
        status = "✓" if rec["success"] else "✗"
        result_repr = json.dumps(rec["result"], ensure_ascii=False)
        tool_name = rec.get("tool_name", "")
        if heavy_logs:
            max_chars = 8000 if tool_name == "trace_query" else 5000 if tool_name == "search_logs" else 1200
        else:
            max_chars = 400
        if len(result_repr) > max_chars:
            result_repr = result_repr[:max_chars] + "...(截断)"
        lines.append(
            f"[{idx}] {status} {rec['tool_name']}({json.dumps(rec['args'], ensure_ascii=False)}) "
            f"→ {result_repr}"
        )
    return "\n".join(lines)


def _safe_json_loads(text: str | None) -> dict[str, Any]:
    """LLM 偶尔会用 ```json 包一下，做一次清洗。"""
    if not text:
        return {}
    text = text.strip()
    if text.startswith("```"):
        # 去掉 ```json ... ``` 标签
        lines = text.splitlines()
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines)
    return json.loads(text)


def _context_block(state: AgentState) -> dict[str, str]:
    """从 State 取出 plan/answer 共用的上下文片段（历史对话 + 用户画像）。

    画像（profile_context）W4-D 接通前恒为空，这里给占位文案，避免 prompt 出现空值。
    """
    return {
        "profile_context": state.get("profile_context") or "（暂无用户画像）",
        "conversation_history": render_history(state.get("episodic_messages")),
    }


async def _exec_tool(tool_call: dict[str, Any], trace_id: str = "") -> ToolCallRecord:
    """执行单次工具调用，返回 ToolCallRecord。coordinator/worker/tool_exec 共用。"""
    tool_name = tool_call.get("tool_name", "")
    args = tool_call.get("args", {}) or {}
    started = time.perf_counter()

    if tool_name not in TOOL_REGISTRY:
        return {
            "tool_name": tool_name,
            "args": args,
            "result": None,
            "success": False,
            "latency_ms": int((time.perf_counter() - started) * 1000),
            "error": f"unknown_tool: {tool_name}",
        }
    try:
        result = await asyncio.to_thread(TOOL_REGISTRY[tool_name], **args)
        return {
            "tool_name": tool_name,
            "args": args,
            "result": result,
            "success": True,
            "latency_ms": int((time.perf_counter() - started) * 1000),
            "error": None,
        }
    except Exception as exc:
        return {
            "tool_name": tool_name,
            "args": args,
            "result": None,
            "success": False,
            "latency_ms": int((time.perf_counter() - started) * 1000),
            "error": f"{type(exc).__name__}: {exc}",
        }


def _format_worker_results(worker_results: list[WorkerResult]) -> str:
    """格式化所有 Worker 结果供 Synthesizer prompt 使用。"""
    if not worker_results:
        return "（无诊断结果）"
    lines = []
    for wr in sorted(worker_results, key=lambda r: r.get("confidence", 0), reverse=True):
        lines.append(
            f"【假设 {wr.get('hypothesis_id', '?')}】{wr.get('hypothesis', '')}\n"
            f"  置信度: {wr.get('confidence', 0):.2f}\n"
            f"  结论: {wr.get('conclusion', '')}\n"
            f"  证据: {json.dumps(wr.get('evidence', []), ensure_ascii=False)[:300]}"
        )
    return "\n\n".join(lines)


# ====================== 节点：load_memory ======================

async def load_memory(state: AgentState) -> dict[str, Any]:
    """装配上下文（4 层记忆汇合点）。

    W4-A：L1 System 按可用工具列表动态拼装（build_system_prompt）。
    W4-B：L3 Episodic 接通跨请求多轮 —— 按 session_id 拉「最近 N 轮原文 + 压缩摘要」。
    W4-D：L2 Profile 接通跨会话画像 —— 按 user_id 拉多版本画像（role 决定 system 语气），
          按 user_query 向量召回历史故障模式。
    episodic 与 profile 都是同步 DB IO，放线程池并发装配，互不阻塞。
    任一层 DB / 模型不可用都已在各自 load_* 内降级（空上下文 / 空画像 / 基线语气）。
    """
    session_id = state.get("session_id", "")
    user_id = state.get("user_id", "")
    user_query = state.get("user_query", "")
    trace_id = state.get("trace_id", "")
    logger.info(f"[load_memory] session={session_id} user={user_id}")

    with span_context(trace_id, "load_memory", {"session_id": session_id, "user_id": user_id}):
        ctx, profile = await asyncio.gather(
            asyncio.to_thread(load_episodic, session_id),
            asyncio.to_thread(load_profile, user_id, user_query),
        )
        update_span(trace_id, "load_memory", output={"role": profile.role})

    return {
        "system_prompt": build_system_prompt(TOOL_DESCRIPTIONS, role=profile.role),
        "profile_context": profile.to_prompt_block(),
        "episodic_messages": ctx.to_messages(),
        "iteration": 0,
        "working_memory": [],
    }


# ====================== 节点：plan ======================

async def plan(state: AgentState) -> dict[str, Any]:
    """LLM 单步决策：call_tool / answer / stop。"""
    trace_id = state.get("trace_id", "")
    iteration = state.get("iteration", 0)

    prompt = load_prompt("plan").format(
        user_query=state["user_query"],
        tool_descriptions=TOOL_DESCRIPTIONS,
        tool_history=_format_tool_history(state.get("working_memory")),
        **_context_block(state),
    )
    logger.info(f"[plan] iteration={iteration}")

    with span_context(trace_id, "plan", {"iteration": iteration}):
        resp = await chat(
            alias="plan",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0,
            max_tokens=512,
        )
        content = resp["choices"][0]["message"]["content"]

        try:
            parsed = _safe_json_loads(content)
        except json.JSONDecodeError as exc:
            logger.error(f"[plan] JSON 解析失败: {exc}\n原始: {content}")
            update_span(trace_id, "plan", output={"error": str(exc)})
            return {
                "plan": content,
                "next_action": "answer",
                "pending_tool_call": None,
                "error": f"plan_json_parse_failed: {exc}",
            }

        next_action = parsed.get("next_action", "answer")
        tool_call = parsed.get("tool_call") if next_action == "call_tool" else None
        update_span(trace_id, "plan", output={"next_action": next_action, "tool_call": tool_call})

    logger.info(f"[plan] decision={next_action} tool={tool_call}")
    return {
        "plan": parsed.get("thought", ""),
        "next_action": next_action,
        "pending_tool_call": tool_call,
    }


# ====================== 节点：tool_exec ======================

async def tool_exec(state: AgentState) -> dict[str, Any]:
    """执行 pending_tool_call，写入 working_memory（追加而非覆盖）。v1 ReAct 专用。"""
    call = state.get("pending_tool_call") or {}
    trace_id = state.get("trace_id", "")

    with span_context(trace_id, "tool_exec", {"tool_name": call.get("tool_name", ""), "args": call.get("args", {})}):
        record = await _exec_tool(call, trace_id)
        update_span(
            trace_id, "tool_exec",
            output={"success": record["success"], "latency_ms": record["latency_ms"]},
        )

    logger.info(
        f"[tool_exec] {record['tool_name']}({record['args']}) success={record['success']} "
        f"latency={record['latency_ms']}ms"
    )
    return {
        "working_memory": [record],  # Annotated[..., add] 会自动 append
        "iteration": state.get("iteration", 0) + 1,
        "pending_tool_call": None,
    }


# ====================== 节点：reflect ======================

async def reflect(state: AgentState) -> dict[str, Any]:
    """判断信息是否够答用户。"""
    iteration = state.get("iteration", 0)
    trace_id = state.get("trace_id", "")

    # 兜底：达到最大轮次强制走 answer，不再调 LLM
    if iteration >= MAX_ITERATIONS:
        logger.warning(f"[reflect] 达到最大轮次 {MAX_ITERATIONS}，强制 answer")
        return {"next_action": "answer"}

    with span_context(trace_id, "reflect", {"iteration": iteration}):
        prompt = load_prompt("reflect").format(
            user_query=state["user_query"],
            tool_history=_format_tool_history(state.get("working_memory")),
            iteration=iteration,
            max_iter=MAX_ITERATIONS,
        )
        resp = await chat(
            alias="reflect",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0,
            max_tokens=200,
        )
        content = resp["choices"][0]["message"]["content"]

        try:
            parsed = _safe_json_loads(content)
            enough = bool(parsed.get("enough", True))
        except json.JSONDecodeError:
            logger.warning(f"[reflect] JSON 解析失败，默认 enough=True\n原始: {content}")
            enough = True

        decision = "answer" if enough else "call_tool"
        update_span(trace_id, "reflect", output={"enough": enough, "next_action": decision})

    logger.info(f"[reflect] enough={enough} → next_action={decision}")
    return {"next_action": decision}


# ====================== 节点：answer ======================

async def answer(state: AgentState) -> dict[str, Any]:
    """生成最终答案（流式）。

    用 get_stream_writer 把 token 增量推到 LangGraph custom 流，HTTP 层据此做 SSE。
    - 通过 graph.astream(stream_mode=[..., "custom"]) 跑时，writer 真实推送 token；
    - 通过 graph.ainvoke 跑时（如烟测），writer 为 no-op，仅累积出 final_answer，向后兼容。
    """
    trace_id = state.get("trace_id", "")

    prompt = load_prompt("answer").format(
        user_query=state["user_query"],
        tool_history=_format_tool_history(state.get("working_memory")),
        **_context_block(state),
    )
    messages = [
        {"role": "system", "content": state.get("system_prompt", "")},
        {"role": "user", "content": prompt},
    ]
    writer = get_stream_writer()

    chunks: list[str] = []
    with span_context(trace_id, "answer", {"query": state.get("user_query", "")}):
        try:
            async for token in chat_stream(alias="answer", messages=messages, temperature=0.3):
                chunks.append(token)
                writer({"type": "token", "data": token})
        except Exception as exc:
            logger.warning(f"[answer] 流式失败，回退非流式: {type(exc).__name__}: {exc}")
            resp = await chat(alias="answer", messages=messages, temperature=0.3, max_tokens=800)
            final = (resp["choices"][0]["message"]["content"] or "").strip()
            writer({"type": "token", "data": final})
            update_span(trace_id, "answer", output={"length": len(final), "fallback": True})
            return {"final_answer": final, "next_action": "stop"}

        final = "".join(chunks).strip()
        update_span(trace_id, "answer", output={"length": len(final)})

    logger.info(f"[answer] 输出 {len(final)} 字（流式）")
    return {"final_answer": final, "next_action": "stop"}


# ====================== 节点：persist_memory ======================

async def persist_memory(state: AgentState) -> dict[str, Any]:
    """落库本轮对话（W4-B）+ 超阈值触发压缩（W4-C）+ 会话画像抽取（W4-D）。

    - 工具调用以精简快照（compact_working_memory）挂在 assistant 行，供后续压缩 / 复盘。
    - 落库走线程池（同步 psycopg），失败由 persist_turn 内部降级为 None，不影响本轮响应。
    - 压缩 / Profile Updater 均为 fire-and-forget 背景任务（create_task），失败不冒泡；
      本轮响应不等它们。（边界：W4 用 create_task，请求结束可能被取消；正式应换后台任务队列。）
    """
    session_id = state.get("session_id", "")
    trace_id = state.get("trace_id", "")
    wm = state.get("working_memory") or []
    tool_calls_json = (
        json.dumps(compact_working_memory(wm), ensure_ascii=False) if wm else None
    )

    with span_context(trace_id, "persist_memory", {"session_id": session_id, "n_tools": len(wm)}):
        turn = await asyncio.to_thread(
            persist_turn,
            session_id,
            state.get("user_query", ""),
            state.get("final_answer") or "",
            tool_calls_json,
        )
        update_span(trace_id, "persist_memory", output={"turn": turn})

    logger.info(
        f"[persist_memory] session={session_id} 落库轮次={turn} "
        f"工具调用={len(wm)} 次"
    )
    if turn:
        _maybe_trigger_compact(state, session_id, turn)
    _trigger_profile_updater(state)
    return {}


def _trigger_profile_updater(state: AgentState) -> None:
    """起后台任务跑 Profile Updater 抽取画像。全程吞异常，绝不影响主链路。"""
    try:
        user_id = state.get("user_id", "")
        if not user_id:
            return
        conversation = (
            f"用户：{state.get('user_query', '')}\n"
            f"助手：{state.get('final_answer') or ''}"
        )
        # fire-and-forget，持引用防 GC（同压缩任务）
        task = asyncio.create_task(run_profile_updater(user_id, conversation))
        _BG_TASKS.add(task)
        task.add_done_callback(_BG_TASKS.discard)
    except Exception as exc:
        logger.warning(
            f"[persist_memory] Profile Updater 触发失败（忽略）: {type(exc).__name__}: {exc}"
        )


def _maybe_trigger_compact(state: AgentState, session_id: str, n_turns: int) -> None:
    """判断是否触发压缩，命中则起后台任务。全程吞异常，绝不影响主链路。"""
    try:
        # 估算当前上下文 token：system + 已装配历史 + 本轮答案
        est_messages = [{"role": "system", "content": state.get("system_prompt", "")}]
        est_messages += state.get("episodic_messages") or []
        est_messages.append(
            {"role": "assistant", "content": state.get("final_answer") or ""}
        )
        tokens = count_tokens(est_messages)
        trigger, reason = should_compact(
            n_turns=n_turns,
            context_tokens=tokens,
            max_context_tokens=resolve_max_context(),
        )
        if not trigger:
            return
        logger.info(
            f"[persist_memory] 触发压缩 reason={reason} (轮数={n_turns}, tokens≈{tokens})"
        )
        # fire-and-forget：压缩调 LLM，放后台，不阻塞本轮 SSE 收尾。
        # 持有强引用防止任务被 GC 回收，完成后回调自动清理。
        task = asyncio.create_task(compact_session(session_id, reason))
        _BG_TASKS.add(task)
        task.add_done_callback(_BG_TASKS.discard)
    except Exception as exc:
        logger.warning(f"[persist_memory] 压缩触发判断失败（忽略）: {type(exc).__name__}: {exc}")


# ====================== v2 节点：coordinator ======================

async def coordinator(state: AgentState) -> dict[str, Any]:
    """生成故障假设列表，驱动并行诊断。（W6 Multi-Agent）"""
    trace_id = state.get("trace_id", "")

    prompt = load_prompt("coordinator").format(
        user_query=state["user_query"],
        tool_descriptions=TOOL_DESCRIPTIONS,
        min_hypotheses=1,
        max_hypotheses=settings.multi_agent_max_hypotheses,
        **_context_block(state),
    )
    logger.info("[coordinator] 生成假设中")

    with span_context(trace_id, "coordinator", {"query": state.get("user_query", "")}):
        resp = await chat(
            alias=settings.model_coordinator,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0,
            max_tokens=512,
        )
        content = resp["choices"][0]["message"]["content"]

        parsed = {}
        try:
            parsed = _safe_json_loads(content)
            hypotheses = parsed.get("hypotheses", [])
        except (json.JSONDecodeError, TypeError):
            logger.warning(f"[coordinator] JSON 解析失败，使用兜底假设\n原始: {content}")
            hypotheses = []

        # 数量兜底
        hypotheses = hypotheses[:settings.multi_agent_max_hypotheses]
        if not hypotheses:
            hypotheses = [{
                "id": "h1",
                "description": state["user_query"],
                "fault_domain": "general",
                "suggested_tools": ["search_logs", "search_sop"],
            }]

        update_span(trace_id, "coordinator", output={
            "n_hypotheses": len(hypotheses),
            "analysis": parsed.get("analysis", "") if isinstance(parsed, dict) else "",
        })

    logger.info(f"[coordinator] 生成 {len(hypotheses)} 个假设")
    return {"hypotheses": hypotheses}


# ====================== v2 节点：worker ======================

async def worker(state: AgentState) -> dict[str, Any]:
    """并行验证单个假设：plan→exec→(plan→exec)→judge。（W6 Multi-Agent）

    通过 Send API 接收 state（含 current_hypothesis），返回 working_memory + worker_results。
    """
    hypothesis = state.get("current_hypothesis", {})
    user_query = state.get("user_query", "")
    trace_id = state.get("trace_id", "")
    h_id = hypothesis.get("id", "h?")
    h_desc = hypothesis.get("description", user_query)
    h_json = json.dumps(hypothesis, ensure_ascii=False)

    tool_records: list[ToolCallRecord] = []
    max_calls = settings.worker_max_tool_calls

    with span_context(trace_id, f"worker_{h_id}", {"hypothesis": h_desc}):
        # 循环：plan → exec，最多 max_calls 轮
        for step in range(max_calls):
            plan_prompt = load_prompt("worker_plan").format(
                hypothesis=h_json,
                fault_domain=hypothesis.get("fault_domain", ""),
                user_query=user_query,
                tool_descriptions=TOOL_DESCRIPTIONS,
                tool_history=_format_tool_history(tool_records),
                profile_context=state.get("profile_context") or "（暂无用户画像）",
            )
            resp = await chat(
                alias=settings.model_worker,
                messages=[{"role": "user", "content": plan_prompt}],
                temperature=0.0,
                max_tokens=256,
            )
            try:
                parsed = _safe_json_loads(resp["choices"][0]["message"]["content"])
            except (json.JSONDecodeError, TypeError):
                break

            tool_call = parsed.get("tool_call")
            if not tool_call:
                # 首轮无工具记录时 LLM 不应跳过——强制使用 suggested_tools 兜底
                if not tool_records and hypothesis.get("suggested_tools"):
                    fallback_tool = hypothesis["suggested_tools"][0]
                    logger.warning(
                        f"[worker_{h_id}] 首轮 LLM 未选工具，强制兜底: {fallback_tool}"
                    )
                    tool_call = {"tool_name": fallback_tool, "args": {"service": user_query.split()[0]}}
                else:
                    break  # 已有记录且 LLM 认为证据够了

            record = await _exec_tool(tool_call, trace_id)
            tool_records.append(record)
            logger.info(
                f"[worker_{h_id}] step={step} tool={record['tool_name']} "
                f"success={record['success']}"
            )

        # 兜底：若循环结束仍未调过 search_sop，自动补一次（保障引用命中）
        called_tools = {r["tool_name"] for r in tool_records}
        if "search_sop" not in called_tools and "search_sop" in TOOL_REGISTRY:
            sop_call = {"tool_name": "search_sop", "args": {"query": h_desc, "top_k": 3}}
            record = await _exec_tool(sop_call, trace_id)
            tool_records.append(record)
            logger.info(f"[worker_{h_id}] 自动补充 search_sop success={record['success']}")

        # judge：评估证据置信度
        judge_prompt = load_prompt("worker_judge").format(
            hypothesis=h_json,
            user_query=user_query,
            tool_history=_format_tool_history(tool_records),
        )
        resp = await chat(
            alias=settings.model_worker,
            messages=[{"role": "user", "content": judge_prompt}],
            temperature=0.0,
            max_tokens=256,
        )
        try:
            judgment = _safe_json_loads(resp["choices"][0]["message"]["content"])
        except (json.JSONDecodeError, TypeError):
            judgment = {}

        # 构建证据摘要（截断防膨胀）
        evidence = []
        for r in tool_records:
            result_str = json.dumps(r["result"], ensure_ascii=False) if r["result"] else ""
            evidence.append({
                "tool": r["tool_name"],
                "success": r["success"],
                "summary": result_str[:200] + ("..." if len(result_str) > 200 else ""),
            })

        worker_result: WorkerResult = {
            "hypothesis_id": h_id,
            "hypothesis": h_desc,
            "evidence": evidence,
            "confidence": float(judgment.get("confidence", 0.5)),
            "conclusion": judgment.get("conclusion", "证据不足，无法判断"),
            "tool_records": tool_records,
        }

        update_span(trace_id, f"worker_{h_id}", output={
            "confidence": worker_result["confidence"],
            "n_tools": len(tool_records),
            "conclusion": worker_result["conclusion"],
        })

    logger.info(
        f"[worker_{h_id}] 完成: confidence={worker_result['confidence']:.2f} "
        f"tools={len(tool_records)}"
    )
    return {
        "working_memory": tool_records,       # add reducer 跨 Worker 累加
        "worker_results": [worker_result],    # add reducer 跨 Worker 累加
    }


# ====================== v2 节点：synthesizer ======================

# synthesizer 用的模型（deepseek）在"证据不足"时会本能地想再调工具，
# 而该节点是终点、无工具执行回路，于是把 <tool_call> 语法当正文漏进 final_answer。
# 这里做纯展示层兜底：闭合块 + 流式截断的未闭合尾巴一并剥掉。
_TOOL_CALL_RE = re.compile(r"<tool_call\b[^>]*>.*?</tool_call>", re.S | re.I)
_TOOL_CALL_DANGLING_RE = re.compile(r"<tool_call\b[^>]*>.*$", re.S | re.I)


def _strip_tool_call_tags(text: str) -> str:
    """剥离 synthesizer 偶发漏出的 <tool_call> 工具调用语法（非性能问题，仅展示噪声）。"""
    if "<tool_call" not in text:
        return text
    cleaned = _TOOL_CALL_RE.sub("", text)          # 先去成对的闭合块
    cleaned = _TOOL_CALL_DANGLING_RE.sub("", cleaned)  # 再去流式截断遗留的未闭合尾巴
    return cleaned.strip()


async def synthesizer(state: AgentState) -> dict[str, Any]:
    """综合所有 Worker 结果，生成最终答案（流式）。（W6 Multi-Agent）"""
    trace_id = state.get("trace_id", "")
    worker_results = state.get("worker_results", [])

    prompt = load_prompt("synthesizer").format(
        user_query=state["user_query"],
        worker_results=_format_worker_results(worker_results),
        tool_history=_format_tool_history(state.get("working_memory")),
        **_context_block(state),
    )
    messages = [
        {"role": "system", "content": state.get("system_prompt", "")},
        {"role": "user", "content": prompt},
    ]
    writer = get_stream_writer()

    chunks: list[str] = []
    with span_context(trace_id, "synthesizer", {"n_workers": len(worker_results)}):
        try:
            async for token in chat_stream(
                alias=settings.model_synthesizer,
                messages=messages,
                temperature=0.3,
            ):
                chunks.append(token)
                writer({"type": "token", "data": token})
        except Exception as exc:
            logger.warning(f"[synthesizer] 流式失败，回退非流式: {type(exc).__name__}: {exc}")
            resp = await chat(
                alias=settings.model_synthesizer,
                messages=messages,
                temperature=0.3,
                max_tokens=800,
            )
            final = (resp["choices"][0]["message"]["content"] or "").strip()
            final = _strip_tool_call_tags(final)
            writer({"type": "token", "data": final})
            update_span(trace_id, "synthesizer", output={"length": len(final), "fallback": True})
            return {"final_answer": final, "next_action": "stop"}

        final = "".join(chunks).strip()
        final = _strip_tool_call_tags(final)
        update_span(trace_id, "synthesizer", output={"length": len(final)})

    logger.info(f"[synthesizer] 输出 {len(final)} 字（流式）")
    return {"final_answer": final, "next_action": "stop"}
