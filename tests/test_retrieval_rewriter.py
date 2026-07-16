"""query_rewriter 单测 —— mock LLM 验证改写逻辑与降级行为。"""

from __future__ import annotations

from unittest.mock import MagicMock, patch


class TestRewriteQuery:
    """query rewriting 核心逻辑。"""

    @patch("opsagent.core.retrieval.query_rewriter.settings")
    @patch("opsagent.core.llm.client.get_router")
    def test_rewrite_combines_original_and_rewritten(self, mock_get_router, mock_settings):
        """改写成功时，返回 original + rewritten 的拼接。"""
        mock_settings.retrieval_rewrite_enabled = True

        # mock LLM 返回
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = "级联超时 cascade timeout 熔断"
        mock_get_router.return_value.completion.return_value = mock_response

        from opsagent.core.retrieval.query_rewriter import rewrite_query

        result = rewrite_query("接口变慢，上游被拖住")
        assert "接口变慢，上游被拖住" in result
        assert "级联超时" in result
        assert "cascade timeout" in result

    @patch("opsagent.core.retrieval.query_rewriter.settings")
    def test_disabled_returns_original(self, mock_settings):
        """开关关闭时直接返回原 query。"""
        mock_settings.retrieval_rewrite_enabled = False

        from opsagent.core.retrieval.query_rewriter import rewrite_query

        result = rewrite_query("DNS 解析失败")
        assert result == "DNS 解析失败"

    @patch("opsagent.core.retrieval.query_rewriter.settings")
    @patch("opsagent.core.llm.client.get_router")
    def test_llm_failure_degrades_to_original(self, mock_get_router, mock_settings):
        """LLM 调用失败时降级返回原 query（不崩）。"""
        mock_settings.retrieval_rewrite_enabled = True
        mock_get_router.return_value.completion.side_effect = RuntimeError("API timeout")

        from opsagent.core.retrieval.query_rewriter import rewrite_query

        result = rewrite_query("Kafka 消费堆积")
        assert result == "Kafka 消费堆积"

    @patch("opsagent.core.retrieval.query_rewriter.settings")
    @patch("opsagent.core.llm.client.get_router")
    def test_empty_rewrite_degrades(self, mock_get_router, mock_settings):
        """LLM 返回空字符串时降级。"""
        mock_settings.retrieval_rewrite_enabled = True

        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = "   "
        mock_get_router.return_value.completion.return_value = mock_response

        from opsagent.core.retrieval.query_rewriter import rewrite_query

        result = rewrite_query("MySQL 慢查询")
        assert result == "MySQL 慢查询"

    @patch("opsagent.core.retrieval.query_rewriter.settings")
    @patch("opsagent.core.llm.client.get_router")
    def test_overly_long_rewrite_degrades(self, mock_get_router, mock_settings):
        """LLM 返回超长结果时降级（防止 prompt injection 或模型乱跑）。"""
        mock_settings.retrieval_rewrite_enabled = True

        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = "x" * 250
        mock_get_router.return_value.completion.return_value = mock_response

        from opsagent.core.retrieval.query_rewriter import rewrite_query

        result = rewrite_query("panic nil pointer")
        assert result == "panic nil pointer"

    @patch("opsagent.core.retrieval.query_rewriter.settings")
    @patch("opsagent.core.llm.client.get_router")
    def test_uses_updater_alias(self, mock_get_router, mock_settings):
        """验证使用 updater 别名（快且便宜）。"""
        mock_settings.retrieval_rewrite_enabled = True

        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = "关键词"
        mock_get_router.return_value.completion.return_value = mock_response

        from opsagent.core.retrieval.query_rewriter import rewrite_query

        rewrite_query("测试")
        call_kwargs = mock_get_router.return_value.completion.call_args
        assert call_kwargs.kwargs.get("model") == "updater" or call_kwargs[1].get("model") == "updater"


class TestPipelineRerank:
    """pipeline rerank 阈值过滤（集成 chunker 的确定性单测）。"""

    def test_rrf_k_setting_default(self):
        """config 默认 RRF k=30。"""
        from opsagent.core.config import Settings

        s = Settings()
        assert s.retrieval_rrf_k == 30

    def test_min_rerank_score_default(self):
        """config 默认 rerank 阈值 0.35。"""
        from opsagent.core.config import Settings

        s = Settings()
        assert s.retrieval_min_rerank_score == 0.35

    def test_rewrite_enabled_default(self):
        """config 默认开启 query rewrite。"""
        from opsagent.core.config import Settings

        s = Settings()
        assert s.retrieval_rewrite_enabled is True
