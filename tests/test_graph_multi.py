"""W6 Multi-Agent 假设驱动并行诊断——节点单测 + 路由逻辑 + 全图集成。

全部 mock LLM / 工具 / 记忆 / 可观测，CI 必跑，无外部依赖。
覆盖要点：
- coordinator: 假设生成、JSON 解析容错、数量上限
- worker: plan→exec→judge 循环、工具失败兜底
- synthesizer: 格式化 + 流式/非流式答案
- builder: _route_to_workers Send 路由
- 全图 v2: load_memory → coordinator → worker×N → synthesizer → persist_memory
"""

import json
from contextlib import contextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from opsagent.core.graph import nodes
from opsagent.core.graph.builder import _route_to_workers
from opsagent.core.graph.nodes import (
    _format_worker_results,
    coordinator,
    synthesizer,
    worker,
)
from opsagent.core.graph.state import AgentState, Hypothesis, WorkerResult

# ====================== Fixtures ======================


def _make_chat_response(content: str) -> dict:
    """构造 LLM chat 返回格式。"""
    return {"choices": [{"message": {"content": content}}]}


def _base_state(**overrides) -> dict[str, Any]:
    """最小可用的 AgentState 字典。"""
    base = {
        "session_id": "test-sess",
        "user_id": "test-user",
        "user_query": "edgectl 网关 5xx 飙升",
        "system_prompt": "你是 OpsAgent",
        "profile_context": "（暂无用户画像）",
        "episodic_messages": [],
        "trace_id": "",
        "working_memory": [],
        "iteration": 0,
    }
    base.update(overrides)
    return base


@pytest.fixture(autouse=True)
def _mock_observability(monkeypatch):
    """全局 mock 可观测——span_context 做 no-op、update_span 静默。
    同时 mock TOOL_REGISTRY 避免兜底逻辑触发真实工具加载。
    """

    @contextmanager
    def _noop_span(*args, **kwargs):
        yield

    monkeypatch.setattr(nodes, "span_context", _noop_span)
    monkeypatch.setattr(nodes, "update_span", lambda *a, **kw: None)
    # mock TOOL_REGISTRY：含 search_sop 键让兜底 in 检查通过，实际执行走 _exec_tool
    monkeypatch.setattr(nodes, "TOOL_REGISTRY", {"search_sop": lambda **kw: {}})


# ====================== coordinator 节点 ======================


class TestCoordinator:
    """Coordinator 节点：生成假设列表。"""

    async def test_parses_hypotheses(self, monkeypatch):
        """正常 JSON → 解析出 hypotheses 列表。"""
        hypotheses = [
            {"id": "h1", "description": "DNS 解析超时", "fault_domain": "dns", "suggested_tools": ["search_logs"]},
            {"id": "h2", "description": "上游限流", "fault_domain": "rate_limit", "suggested_tools": ["get_service_metrics"]},
        ]
        resp = _make_chat_response(json.dumps({"analysis": "初步分析", "hypotheses": hypotheses}))
        monkeypatch.setattr(nodes, "chat", AsyncMock(return_value=resp))

        result = await coordinator(_base_state())
        assert len(result["hypotheses"]) == 2
        assert result["hypotheses"][0]["id"] == "h1"
        assert result["hypotheses"][1]["fault_domain"] == "rate_limit"

    async def test_fallback_on_bad_json(self, monkeypatch):
        """LLM 返回非法 JSON → 降级为 1 个兜底假设。"""
        resp = _make_chat_response("这不是合法JSON{{{")
        monkeypatch.setattr(nodes, "chat", AsyncMock(return_value=resp))

        result = await coordinator(_base_state(user_query="网关超时"))
        hyps = result["hypotheses"]
        assert len(hyps) == 1
        assert hyps[0]["id"] == "h1"
        assert "网关超时" in hyps[0]["description"]

    async def test_caps_max_hypotheses(self, monkeypatch):
        """假设数量超限 → 截断到 max。"""
        hypotheses = [
            {"id": f"h{i}", "description": f"假设{i}", "fault_domain": "x", "suggested_tools": []}
            for i in range(10)
        ]
        resp = _make_chat_response(json.dumps({"hypotheses": hypotheses}))
        monkeypatch.setattr(nodes, "chat", AsyncMock(return_value=resp))

        # 设置 max=2
        monkeypatch.setattr(nodes.settings, "multi_agent_max_hypotheses", 2)
        result = await coordinator(_base_state())
        assert len(result["hypotheses"]) == 2

    async def test_empty_hypotheses_gets_fallback(self, monkeypatch):
        """LLM 返回空假设列表 → 降级为 1 个兜底。"""
        resp = _make_chat_response(json.dumps({"analysis": "无法分析", "hypotheses": []}))
        monkeypatch.setattr(nodes, "chat", AsyncMock(return_value=resp))

        result = await coordinator(_base_state())
        assert len(result["hypotheses"]) == 1
        assert result["hypotheses"][0]["fault_domain"] == "general"


# ====================== worker 节点 ======================


class TestWorker:
    """Worker 节点：plan→exec→judge 循环。"""

    def _hypothesis(self, **overrides) -> Hypothesis:
        h: Hypothesis = {
            "id": "h1",
            "description": "DNS 超时导致 5xx",
            "fault_domain": "dns",
            "suggested_tools": ["search_logs"],
        }
        h.update(overrides)  # type: ignore[typeddict-item]
        return h

    async def test_plan_exec_judge_normal(self, monkeypatch):
        """正常路径：plan 选工具 → exec 成功 → judge 给置信度。"""
        call_count = {"n": 0}

        async def _fake_chat(**kwargs):
            call_count["n"] += 1
            msgs = kwargs.get("messages", [])
            content = msgs[0]["content"] if msgs else ""

            if call_count["n"] == 1:
                # 第一次 plan：选工具
                return _make_chat_response(json.dumps({
                    "thought": "查日志确认 DNS 超时",
                    "tool_call": {"tool_name": "search_logs", "args": {"service": "edgectl", "keyword": "DNS"}},
                }))
            elif call_count["n"] == 2:
                # 第二次 plan：不再需要工具
                return _make_chat_response(json.dumps({
                    "thought": "证据充足",
                    "tool_call": None,
                }))
            else:
                # judge
                return _make_chat_response(json.dumps({
                    "evidence_summary": "日志中有 DNS 超时错误",
                    "confidence": 0.85,
                    "conclusion": "DNS 解析超时导致 5xx 飙升",
                    "supporting": True,
                }))

        # mock 工具执行
        async def _fake_exec_tool(tool_call, trace_id=""):
            return {
                "tool_name": "search_logs",
                "args": {"service": "edgectl", "keyword": "DNS"},
                "result": {"data": [{"msg": "DNS timeout"}], "meta": {"hit": 3, "returned": 3}},
                "success": True,
                "latency_ms": 50,
                "error": None,
            }

        monkeypatch.setattr(nodes, "chat", _fake_chat)
        monkeypatch.setattr(nodes, "_exec_tool", _fake_exec_tool)
        monkeypatch.setattr(nodes.settings, "worker_max_tool_calls", 2)

        state = _base_state(current_hypothesis=self._hypothesis())
        result = await worker(state)

        # 验证输出
        assert len(result["worker_results"]) == 1
        wr = result["worker_results"][0]
        assert wr["hypothesis_id"] == "h1"
        assert wr["confidence"] == pytest.approx(0.85)
        assert "DNS" in wr["conclusion"]
        # 调了 1 次 search_logs + 兜底补 1 次 search_sop = 2
        assert len(result["working_memory"]) == 2
        assert result["working_memory"][0]["tool_name"] == "search_logs"

    async def test_second_round_skips_tool(self, monkeypatch):
        """首轮调完工具后，第二轮 plan 说不需要 → 跳过 exec → judge。"""
        call_count = {"n": 0}

        async def _fake_chat(**kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                # 首轮：选工具
                return _make_chat_response(json.dumps({
                    "thought": "先查日志",
                    "tool_call": {"tool_name": "search_logs", "args": {"service": "svc"}},
                }))
            elif call_count["n"] == 2:
                # 第二轮：证据够了
                return _make_chat_response(json.dumps({"thought": "无需工具", "tool_call": None}))
            else:
                # judge
                return _make_chat_response(json.dumps({
                    "confidence": 0.6,
                    "conclusion": "有一定证据",
                }))

        async def _fake_exec(tool_call, trace_id=""):
            return {
                "tool_name": "search_logs",
                "args": {"service": "svc"},
                "result": {"data": [], "meta": {"hit": 0}},
                "success": True,
                "latency_ms": 5,
                "error": None,
            }

        monkeypatch.setattr(nodes, "chat", _fake_chat)
        monkeypatch.setattr(nodes, "_exec_tool", _fake_exec)
        monkeypatch.setattr(nodes.settings, "worker_max_tool_calls", 2)

        state = _base_state(current_hypothesis=self._hypothesis())
        result = await worker(state)

        wr = result["worker_results"][0]
        assert wr["confidence"] == pytest.approx(0.6)
        # 调了 1 次工具（首轮）+ 兜底补 search_sop = 2
        assert len(result["working_memory"]) == 2

    async def test_tool_failure_still_returns_result(self, monkeypatch):
        """工具执行失败 → Worker 仍返回结果（confidence 较低）。"""
        call_count = {"n": 0}

        async def _fake_chat(**kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return _make_chat_response(json.dumps({
                    "thought": "查日志",
                    "tool_call": {"tool_name": "search_logs", "args": {"service": "svc"}},
                }))
            elif call_count["n"] == 2:
                return _make_chat_response(json.dumps({"thought": "已尝试", "tool_call": None}))
            else:
                return _make_chat_response(json.dumps({
                    "confidence": 0.1,
                    "conclusion": "工具失败，无法验证",
                }))

        async def _fail_exec(tool_call, trace_id=""):
            return {
                "tool_name": "search_logs",
                "args": {},
                "result": None,
                "success": False,
                "latency_ms": 10,
                "error": "ConnectionError: PG 不可达",
            }

        monkeypatch.setattr(nodes, "chat", _fake_chat)
        monkeypatch.setattr(nodes, "_exec_tool", _fail_exec)

        state = _base_state(current_hypothesis=self._hypothesis())
        result = await worker(state)

        wr = result["worker_results"][0]
        assert wr["confidence"] == pytest.approx(0.1)
        # 1 次 search_logs 失败 + 兜底补 search_sop 也失败 = 2
        assert len(result["working_memory"]) == 2
        assert result["working_memory"][0]["success"] is False

    async def test_judge_json_fail_gives_default(self, monkeypatch):
        """judge 返回非法 JSON → confidence 默认 0.5。"""
        call_count = {"n": 0}

        async def _fake_chat(**kwargs):
            call_count["n"] += 1
            if call_count["n"] <= 2:
                # 前两轮 plan：选工具后不再需要
                if call_count["n"] == 1:
                    return _make_chat_response(json.dumps({
                        "thought": "查日志", "tool_call": {"tool_name": "search_logs", "args": {"service": "svc"}}
                    }))
                return _make_chat_response(json.dumps({"thought": "够了", "tool_call": None}))
            else:
                # judge 返回非法 JSON
                return _make_chat_response("不是JSON")

        async def _fake_exec(tool_call, trace_id=""):
            return {
                "tool_name": tool_call.get("tool_name", ""),
                "args": tool_call.get("args", {}),
                "result": {"data": []},
                "success": True,
                "latency_ms": 5,
                "error": None,
            }

        monkeypatch.setattr(nodes, "chat", _fake_chat)
        monkeypatch.setattr(nodes, "_exec_tool", _fake_exec)

        state = _base_state(current_hypothesis=self._hypothesis())
        result = await worker(state)

        wr = result["worker_results"][0]
        assert wr["confidence"] == pytest.approx(0.5)  # 默认值
        assert wr["conclusion"] == "证据不足，无法判断"  # 默认结论


# ====================== synthesizer 节点 ======================


class TestSynthesizer:
    """Synthesizer 节点：汇总 + 流式输出。"""

    def _worker_results(self) -> list[WorkerResult]:
        return [
            {
                "hypothesis_id": "h1",
                "hypothesis": "DNS 超时",
                "evidence": [{"tool": "search_logs", "success": True, "summary": "3 条 DNS 超时日志"}],
                "confidence": 0.85,
                "conclusion": "DNS 解析超时导致 5xx",
                "tool_records": [],
            },
            {
                "hypothesis_id": "h2",
                "hypothesis": "上游限流",
                "evidence": [],
                "confidence": 0.2,
                "conclusion": "未发现限流证据",
                "tool_records": [],
            },
        ]

    async def test_stream_answer(self, monkeypatch):
        """流式正常路径：token 逐个推出，final_answer 拼接正确。"""
        tokens = ["DNS ", "解析超时", "是根因"]

        async def _fake_stream(**kwargs):
            for t in tokens:
                yield t

        monkeypatch.setattr(nodes, "chat_stream", _fake_stream)
        monkeypatch.setattr(nodes, "get_stream_writer", lambda: (lambda x: None))

        state = _base_state(worker_results=self._worker_results())
        result = await synthesizer(state)

        assert result["final_answer"] == "DNS 解析超时是根因"
        assert result["next_action"] == "stop"

    async def test_fallback_to_non_stream(self, monkeypatch):
        """流式失败 → 回退非流式。"""

        async def _boom_stream(**kwargs):
            raise RuntimeError("流式不可用")
            yield  # noqa: unreachable — 保持 async generator 语法

        resp = _make_chat_response("非流式回退答案")
        monkeypatch.setattr(nodes, "chat_stream", _boom_stream)
        monkeypatch.setattr(nodes, "chat", AsyncMock(return_value=resp))
        monkeypatch.setattr(nodes, "get_stream_writer", lambda: (lambda x: None))

        state = _base_state(worker_results=self._worker_results())
        result = await synthesizer(state)

        assert result["final_answer"] == "非流式回退答案"


# ====================== 辅助函数 ======================


class TestHelpers:
    """_format_worker_results / _route_to_workers 等辅助逻辑。"""

    def test_format_worker_results_sorted_by_confidence(self):
        """按置信度降序排列。"""
        results: list[WorkerResult] = [
            {"hypothesis_id": "h2", "hypothesis": "低", "evidence": [], "confidence": 0.2,
             "conclusion": "不太可能", "tool_records": []},
            {"hypothesis_id": "h1", "hypothesis": "高", "evidence": [], "confidence": 0.9,
             "conclusion": "很可能", "tool_records": []},
        ]
        text = _format_worker_results(results)
        # h1(0.9) 应排在 h2(0.2) 前面
        h1_pos = text.index("假设 h1")
        h2_pos = text.index("假设 h2")
        assert h1_pos < h2_pos

    def test_format_worker_results_empty(self):
        """空列表 → 占位文案。"""
        assert "无诊断结果" in _format_worker_results([])

    def test_route_to_workers_creates_sends(self):
        """有假设 → 每个假设一个 Send。"""
        state: dict[str, Any] = _base_state(hypotheses=[
            {"id": "h1", "description": "假设1", "fault_domain": "dns", "suggested_tools": []},
            {"id": "h2", "description": "假设2", "fault_domain": "net", "suggested_tools": []},
        ])
        sends = _route_to_workers(state)  # type: ignore[arg-type]
        assert len(sends) == 2
        # Send 对象的 node 属性和 state 属性
        assert sends[0].node == "worker"
        assert sends[1].node == "worker"
        assert sends[0].arg["current_hypothesis"]["id"] == "h1"
        assert sends[1].arg["current_hypothesis"]["id"] == "h2"

    def test_route_to_workers_empty_hypotheses(self):
        """无假设 → 直接 Send 到 synthesizer。"""
        state: dict[str, Any] = _base_state(hypotheses=[])
        sends = _route_to_workers(state)  # type: ignore[arg-type]
        assert len(sends) == 1
        assert sends[0].node == "synthesizer"


# ====================== 全图 v2 集成 ======================


class TestV2GraphIntegration:
    """v2 完整图集成测试（全 mock）。"""

    async def test_full_graph_v2_end_to_end(self, monkeypatch):
        """load_memory → coordinator → worker×2 → synthesizer → persist_memory。

        验证：Send fan-out 正确启动多 worker、worker_results 通过 add reducer 汇总、
        final_answer 由 synthesizer 写入。
        """
        # mock load_memory 依赖
        monkeypatch.setattr(nodes, "load_episodic", lambda sid, **kw: _FakeEpisodicCtx())
        monkeypatch.setattr(nodes, "load_profile", lambda uid, query=None: _FakeProfile())
        monkeypatch.setattr(nodes, "build_system_prompt", lambda tools, role=None: "mock system")

        # mock persist_memory 依赖
        monkeypatch.setattr(nodes, "persist_turn", lambda *a, **kw: 1)
        monkeypatch.setattr(nodes, "compact_working_memory", lambda wm, **kw: [])
        monkeypatch.setattr(nodes, "should_compact", lambda **kw: (False, "no"))
        monkeypatch.setattr(nodes, "run_profile_updater", AsyncMock())

        # mock _exec_tool（Worker 兜底 search_sop 也走这里）
        async def _fake_exec(tool_call, trace_id=""):
            return {
                "tool_name": tool_call.get("tool_name", ""),
                "args": tool_call.get("args", {}),
                "result": {"data": [{"doc_id": "sop-test"}], "meta": {}},
                "success": True,
                "latency_ms": 5,
                "error": None,
            }

        monkeypatch.setattr(nodes, "_exec_tool", _fake_exec)

        # 路由 LLM 调用（用调用序号区分：第 1 次=coordinator，后续=worker）
        call_count = {"n": 0}

        async def _route_chat(**kwargs):
            call_count["n"] += 1
            n = call_count["n"]

            # 第 1 次调用：coordinator
            if n == 1:
                return _make_chat_response(json.dumps({
                    "analysis": "可能 DNS 或限流",
                    "hypotheses": [
                        {"id": "h1", "description": "DNS 超时", "fault_domain": "dns",
                         "suggested_tools": ["search_logs"]},
                        {"id": "h2", "description": "上游限流", "fault_domain": "rate_limit",
                         "suggested_tools": ["get_service_metrics"]},
                    ],
                }))
            # Worker plan 首轮：选工具
            # Worker plan 后续：不再需要
            # Worker judge：给置信度
            # 模式：每个 Worker 3 次 LLM 调用（plan选工具 → plan不需要 → judge）
            phase = (n - 2) % 3  # 0=首轮plan, 1=二轮plan, 2=judge
            if phase == 0:
                return _make_chat_response(json.dumps({
                    "thought": "查日志确认",
                    "tool_call": {"tool_name": "search_logs", "args": {"service": "edgectl"}},
                }))
            elif phase == 1:
                return _make_chat_response(json.dumps({
                    "thought": "证据够了",
                    "tool_call": None,
                }))
            else:
                return _make_chat_response(json.dumps({
                    "evidence_summary": "找到相关证据",
                    "confidence": 0.7,
                    "conclusion": "有一定证据支持",
                    "supporting": True,
                }))

        async def _fake_stream(**kwargs):
            for t in ["综合", "诊断", "结论"]:
                yield t

        monkeypatch.setattr(nodes, "chat", _route_chat)
        monkeypatch.setattr(nodes, "chat_stream", _fake_stream)
        monkeypatch.setattr(nodes, "get_stream_writer", lambda: (lambda x: None))

        # 用 v2 图
        monkeypatch.setattr(nodes.settings, "graph_version", "v2")
        monkeypatch.setattr(nodes.settings, "model_coordinator", "plan")
        monkeypatch.setattr(nodes.settings, "model_worker", "plan")
        monkeypatch.setattr(nodes.settings, "model_synthesizer", "answer")

        # 构建 v2 图（绕过 lru_cache）
        from opsagent.core.graph.builder import _build_graph_v2

        graph = _build_graph_v2()

        # 运行
        initial: dict[str, Any] = {
            "session_id": "test-v2-e2e",
            "user_id": "test-user",
            "user_query": "edgectl 网关 5xx 飙升",
        }
        final_state = await graph.ainvoke(initial)

        # 验证
        assert final_state.get("final_answer") == "综合诊断结论"
        assert len(final_state.get("hypotheses", [])) == 2

        # worker_results 应有 2 条（2 个 Worker 各一条，通过 add reducer 合并）
        wrs = final_state.get("worker_results", [])
        assert len(wrs) == 2
        h_ids = {wr["hypothesis_id"] for wr in wrs}
        assert h_ids == {"h1", "h2"}

        # 每个 worker confidence 都是 0.7
        for wr in wrs:
            assert wr["confidence"] == pytest.approx(0.7)


# ====================== 测试用假对象 ======================


class _FakeEpisodicCtx:
    """mock load_episodic 返回值。"""

    def to_messages(self):
        return []

    def is_empty(self):
        return True


class _FakeProfile:
    """mock load_profile 返回值。"""

    role = None

    def to_prompt_block(self):
        return "（暂无用户画像）"
