"""eval/metrics/scorer.py 单测 —— 纯内存、无 IO / 无 LLM。"""

from __future__ import annotations

import pytest

from eval.metrics.scorer import (
    EvalResult,
    citation_score,
    conclusion_score,
    forbidden_penalty,
    score_case,
    tool_sequence_score,
)

# ====================== tool_sequence_score ======================


class TestToolSequenceScore:
    """工具调用 Recall 打分测试。"""

    def test_both_empty(self):
        """actual 和 expected 均空 → 满分。"""
        score, matched, missing, extra = tool_sequence_score([], [])
        assert score == 1.0
        assert matched == [] and missing == [] and extra == []

    def test_expected_empty(self):
        """不考核工具时，任何 actual 都满分。"""
        actual = [{"tool_name": "search_logs", "args": {}}]
        score, _, _, extra = tool_sequence_score(actual, [])
        assert score == 1.0
        assert extra == ["search_logs"]

    def test_actual_empty(self):
        """actual 空但 expected 非空 → 0 分。"""
        expected = [{"tool": "search_logs", "args_match": {}}]
        score, _, missing, _ = tool_sequence_score([], expected)
        assert score == 0.0
        assert missing == ["search_logs"]

    def test_perfect_match(self):
        """完全匹配 → Recall = 1.0。"""
        actual = [
            {"tool_name": "search_logs", "args": {"service": "edgectl-backend-http"}},
            {"tool_name": "search_sop", "args": {}},
        ]
        expected = [
            {"tool": "search_logs", "args_match": {"service": "edgectl-backend-http"}},
            {"tool": "search_sop", "args_match": {}},
        ]
        score, matched, missing, extra = tool_sequence_score(actual, expected)
        assert score == 1.0
        assert len(matched) == 2
        assert missing == [] and extra == []

    def test_partial_match(self):
        """命中 1/2 expected、多一个 extra → Recall=0.5（extra 不扣分）。"""
        actual = [
            {"tool_name": "search_logs", "args": {"service": "backend"}},
            {"tool_name": "query_metrics", "args": {}},
        ]
        expected = [
            {"tool": "search_logs", "args_match": {}},
            {"tool": "search_sop", "args_match": {}},
        ]
        score, matched, missing, extra = tool_sequence_score(actual, expected)
        # matched=1, Recall=1/2=0.5（extra 不影响分数）
        assert score == pytest.approx(0.5)
        assert matched == ["search_logs"]
        assert missing == ["search_sop"]
        assert extra == ["query_metrics"]

    def test_extra_calls_no_penalty(self):
        """多余的正确工具调用不扣分 → 仍满分。"""
        actual = [
            {"tool_name": "search_logs", "args": {"service": "backend-http"}},
            {"tool_name": "search_logs", "args": {"service": "admin"}},
            {"tool_name": "trace_query", "args": {}},
            {"tool_name": "search_sop", "args": {}},
        ]
        expected = [
            {"tool": "search_logs", "args_match": {"service": "backend-http"}},
        ]
        score, matched, missing, extra = tool_sequence_score(actual, expected)
        # Recall = 1/1 = 1.0，多调 3 次不扣分
        assert score == 1.0
        assert matched == ["search_logs"]
        assert missing == []
        assert len(extra) == 3

    def test_args_substring_match(self):
        """args_match 做子串匹配（不区分大小写）。"""
        actual = [{"tool_name": "search_logs", "args": {"service": "edgectl-Backend-HTTP"}}]
        expected = [{"tool": "search_logs", "args_match": {"service": "backend-http"}}]
        score, matched, _, _ = tool_sequence_score(actual, expected)
        assert score == 1.0
        assert matched == ["search_logs"]

    def test_args_mismatch(self):
        """args 不匹配 → 不算命中。"""
        actual = [{"tool_name": "search_logs", "args": {"service": "admin"}}]
        expected = [{"tool": "search_logs", "args_match": {"service": "backend-http"}}]
        score, _, missing, _ = tool_sequence_score(actual, expected)
        assert score == 0.0
        assert missing == ["search_logs"]

    def test_tools_list_hit(self):
        """tools 列表格式：命中其中任一即满分。"""
        actual = [{"tool_name": "trace_query", "args": {}}]
        expected = [
            {"tools": ["search_logs", "trace_query", "get_service_metrics"], "args_match": {}}
        ]
        score, matched, missing, extra = tool_sequence_score(actual, expected)
        assert score == 1.0
        assert matched == ["trace_query"]
        assert missing == []

    def test_tools_list_miss(self):
        """tools 列表格式：一个都没命中 → 0 分。"""
        actual = [{"tool_name": "kb_search", "args": {}}]
        expected = [
            {"tools": ["search_logs", "trace_query", "get_service_metrics"], "args_match": {}}
        ]
        score, matched, missing, extra = tool_sequence_score(actual, expected)
        assert score == 0.0
        assert missing == ["search_logs|trace_query|get_service_metrics"]

    def test_tools_list_with_args(self):
        """tools 列表 + args_match：需工具名和参数同时满足。"""
        actual = [{"tool_name": "search_logs", "args": {"service": "edgectl"}}]
        expected = [
            {"tools": ["search_logs", "trace_query"], "args_match": {"service": "edgectl"}}
        ]
        score, matched, _, _ = tool_sequence_score(actual, expected)
        assert score == 1.0
        assert matched == ["search_logs"]


# ====================== citation_score ======================


class TestCitationScore:
    """引用命中测试。"""

    def test_no_expected(self):
        """无期望引用 → 满分。"""
        score, _ = citation_score("答案", [], [])
        assert score == 1.0

    def test_hit_from_answer_text(self):
        """从 final_answer 文本中提取到引用。"""
        answer = "根据 sop-f1-cascade-timeout 文档..."
        score, matched = citation_score(answer, [], ["sop-f1-cascade-timeout"])
        assert score == 1.0
        assert "sop-f1-cascade-timeout" in matched

    def test_hit_from_working_memory(self):
        """从 working_memory 中的 search_sop 结果提取引用。"""
        wm = [
            {
                "tool_name": "search_sop",
                "args": {},
                "result": {
                    "data": [{"doc_id": "sop-f1-cascade-timeout", "title": "级联超时"}]
                },
            }
        ]
        score, _ = citation_score("简单回答", wm, ["sop-f1-cascade-timeout"])
        assert score == 1.0

    def test_no_hit(self):
        """完全未命中 → 0 分。"""
        score, matched = citation_score("无关内容", [], ["sop-f1-cascade-timeout"])
        assert score == 0.0
        assert matched == []

    def test_partial_match_substring(self):
        """子串匹配：expected 在 actual 中出现即命中。"""
        wm = [
            {
                "tool_name": "kb_search",
                "args": {},
                "result": [{"doc_id": "pm-001-cascade-timeout-full-report"}],
            }
        ]
        score, _ = citation_score("", wm, ["pm-001-cascade-timeout"])
        assert score == 1.0

    def test_gradient_scoring(self):
        """梯度评分：命中 1/2 → 0.5。"""
        wm = [
            {
                "tool_name": "search_sop",
                "args": {},
                "result": {"data": [{"doc_id": "sop-f1-cascade-timeout"}]},
            }
        ]
        # 期望 2 个引用，只命中 1 个 → 0.5
        score, matched = citation_score(
            "简单回答", wm, ["sop-f1-cascade-timeout", "pm-001-cascade-timeout"]
        )
        assert score == pytest.approx(0.5)
        assert len(matched) == 1


# ====================== conclusion_score ======================


class TestConclusionScore:
    """结论关键词命中测试。"""

    def test_no_keywords(self):
        """无期望关键词 → 满分。"""
        score, _ = conclusion_score("答案", [], 0)
        assert score == 1.0

    def test_all_hit(self):
        """全部命中 → 1.0。"""
        score, matched = conclusion_score(
            "出现了超时和雪崩，建议加熔断",
            ["超时", "雪崩", "熔断"],
            3,
        )
        assert score == 1.0
        assert len(matched) == 3

    def test_partial_hit(self):
        """命中 2/3，min_required=3 → 2/3。"""
        score, _ = conclusion_score(
            "出现了超时和雪崩",
            ["超时", "雪崩", "熔断"],
            3,
        )
        assert score == pytest.approx(2 / 3, abs=0.01)

    def test_over_min(self):
        """命中超过 min → 封顶 1.0。"""
        score, _ = conclusion_score(
            "超时、雪崩、熔断、降级全有",
            ["超时", "雪崩", "熔断", "降级"],
            2,
        )
        assert score == 1.0

    def test_empty_answer(self):
        """空回答 → 0 分。"""
        score, _ = conclusion_score("", ["超时"], 1)
        assert score == 0.0


# ====================== forbidden_penalty ======================


class TestForbiddenPenalty:
    """禁忌词扣分测试。"""

    def test_no_forbidden(self):
        """无禁忌词列表 → 0 罚分。"""
        penalty, _ = forbidden_penalty("答案", [])
        assert penalty == 0.0

    def test_not_triggered(self):
        """未命中 → 0。"""
        penalty, triggered = forbidden_penalty("超时雪崩", ["kafka", "DNS"])
        assert penalty == 0.0
        assert triggered == []

    def test_single_trigger(self):
        """命中 1 个 → 0.1。"""
        penalty, triggered = forbidden_penalty("看看 kafka 堆积", ["kafka", "DNS"])
        assert penalty == pytest.approx(0.1)
        assert triggered == ["kafka"]

    def test_multiple_trigger(self):
        """命中 2 个 → 0.2（累加制）。"""
        penalty, triggered = forbidden_penalty("kafka 加 DNS 问题", ["kafka", "DNS"])
        assert penalty == pytest.approx(0.2)
        assert len(triggered) == 2

    def test_cap_at_03(self):
        """命中 4 个以上仍封顶 0.3。"""
        penalty, triggered = forbidden_penalty(
            "kafka DNS panic 超时 全有", ["kafka", "DNS", "panic", "超时"]
        )
        assert penalty == pytest.approx(0.3)
        assert len(triggered) == 4


# ====================== score_case ======================


class TestScoreCase:
    """主入口 score_case 集成测试。"""

    def _make_case(self, **overrides):
        """构造最小评测 case。"""
        base = {
            "id": "T001",
            "expected_tool_sequence": [
                {"tool": "search_logs", "args_match": {}},
            ],
            "expected_citations": ["sop-f1-cascade-timeout"],
            "expected_keywords": ["超时", "级联"],
            "expected_keywords_min": 2,
            "forbidden_keywords": ["kafka"],
        }
        base.update(overrides)
        return base

    def test_perfect_score(self):
        """完美回答 → 接近 1.0。"""
        case = self._make_case()
        actual_calls = [{"tool_name": "search_logs", "args": {}}]
        answer = "根据 sop-f1-cascade-timeout，出现了超时级联问题"
        result = score_case(case, actual_calls, answer)
        assert isinstance(result, EvalResult)
        assert result.total_score >= 0.9

    def test_zero_score(self):
        """完全不匹配 → 0 分。"""
        case = self._make_case()
        result = score_case(case, [], "无关回答")
        assert result.total_score == 0.0

    def test_forbidden_deduction(self):
        """触发 forbidden → 总分被扣（累加制 0.1/词）。"""
        case = self._make_case()
        actual_calls = [{"tool_name": "search_logs", "args": {}}]
        answer = "看起来是 kafka 超时级联，参考 sop-f1-cascade-timeout"
        result = score_case(case, actual_calls, answer)
        # 触发 1 个 forbidden 词 → penalty=0.1
        assert result.forbidden_penalty == pytest.approx(0.1)
        assert result.total_score < 1.0

    def test_result_fields(self):
        """EvalResult 包含所有明细字段。"""
        case = self._make_case()
        result = score_case(case, [], "")
        assert result.case_id == "T001"
        assert hasattr(result, "matched_tools")
        assert hasattr(result, "missing_tools")
        assert hasattr(result, "matched_citations")
        assert hasattr(result, "matched_keywords")
        assert hasattr(result, "triggered_forbidden")

    def test_difficulty_easy_weights(self):
        """easy 难度：tool 权重 0.4, cite 0.3, conc 0.3。"""
        case = self._make_case(difficulty="easy")
        actual_calls = [{"tool_name": "search_logs", "args": {}}]
        answer = "根据 sop-f1-cascade-timeout，出现了超时级联问题"
        result = score_case(case, actual_calls, answer)
        # tool=1.0, cite=1.0, conc=1.0 → 0.4+0.3+0.3=1.0
        assert result.total_score == pytest.approx(1.0)

    def test_difficulty_medium_weights(self):
        """medium 难度：tool 权重降至 0.2。"""
        case = self._make_case(difficulty="medium")
        # tool miss → tool_score=0, cite=1.0, conc=1.0
        answer = "根据 sop-f1-cascade-timeout，出现了超时级联问题"
        result = score_case(case, [], answer)
        # 0.2*0 + 0.4*1.0 + 0.4*1.0 = 0.8
        assert result.total_score == pytest.approx(0.8)

    def test_difficulty_hard_weights(self):
        """hard 难度：tool 权重仅 0.1，结论权重 0.5。"""
        case = self._make_case(difficulty="hard")
        # tool miss → tool_score=0, cite=1.0, conc=1.0
        answer = "根据 sop-f1-cascade-timeout，出现了超时级联问题"
        result = score_case(case, [], answer)
        # 0.1*0 + 0.4*1.0 + 0.5*1.0 = 0.9
        assert result.total_score == pytest.approx(0.9)

    def test_difficulty_default_is_easy(self):
        """无 difficulty 字段默认按 easy 权重。"""
        case = self._make_case()
        # 不传 difficulty → 默认 easy
        assert "difficulty" not in case
        actual_calls = [{"tool_name": "search_logs", "args": {}}]
        answer = "根据 sop-f1-cascade-timeout，出现了超时级联问题"
        result = score_case(case, actual_calls, answer)
        assert result.total_score == pytest.approx(1.0)

    def test_tools_list_in_score_case(self):
        """score_case 支持 tools 列表格式的 expected。"""
        case = self._make_case(
            difficulty="medium",
            expected_tool_sequence=[
                {"tools": ["search_logs", "trace_query", "get_service_metrics"], "args_match": {}}
            ],
        )
        # 调了 trace_query → 命中 tools 列表
        actual_calls = [{"tool_name": "trace_query", "args": {}}]
        answer = "根据 sop-f1-cascade-timeout，出现了超时级联问题"
        result = score_case(case, actual_calls, answer)
        assert result.tool_score == 1.0
        # medium: 0.2*1.0 + 0.4*1.0 + 0.4*1.0 = 1.0
        assert result.total_score == pytest.approx(1.0)
