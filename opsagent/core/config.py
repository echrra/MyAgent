"""统一配置加载器：从 .env 读取所有运行时参数。

业务代码任何地方拿配置都走 `from opsagent.core.config import settings`，
避免直接读 os.environ 造成的散乱与拼错。
"""

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    """全局配置，字段名严格对应 .env 中的 KEY。"""

    # ---------- 应用基础 ----------
    app_env: str = Field(default="dev")
    app_log_level: str = Field(default="INFO")
    app_host: str = Field(default="0.0.0.0")
    app_port: int = Field(default=8000)

    # ---------- 模型 API ----------
    deepseek_api_key: str = Field(default="")
    deepseek_base_url: str = Field(default="https://api.deepseek.com")
    dashscope_api_key: str = Field(default="")
    dashscope_base_url: str = Field(
        default="https://dashscope.aliyuncs.com/compatible-mode/v1"
    )

    # ---------- 模型路由别名（默认值与 litellm_config.yaml 对齐）----------
    model_plan: str = Field(default="plan")
    model_reflect: str = Field(default="reflect")
    model_answer: str = Field(default="answer")
    model_updater: str = Field(default="updater")
    model_fallback: str = Field(default="fallback")

    # ---------- 记忆参数 ----------
    memory_compact_token_ratio: float = Field(default=0.7)
    memory_compact_round_count: int = Field(default=8)
    max_agent_iterations: int = Field(default=5)
    # 模型上下文窗口兜底值：自定义别名无法从 SDK 获取上限，
    # 直接用此值算压缩阈值（DeepSeek 64K 可放宽 / Qwen 32K 收紧，按主模型保守取 32K）
    memory_max_context_tokens: int = Field(default=32768)
    # 装配上下文时保留的最近完整轮数（更早的轮走压缩摘要）
    memory_keep_recent_turns: int = Field(default=3)
    # 一级摘要累计达此条数触发递归压缩（多个旧摘要合并为更高层摘要）
    memory_summary_merge_count: int = Field(default=4)
    # 装配画像时按 query 召回的历史故障模式条数
    memory_fault_pattern_top_n: int = Field(default=3)

    # ---------- W3 工具治理（Tool 抽象层默认值，可被单个工具注册时覆盖）----------
    # 单次工具调用超时（秒）；超时仅「停止等待」，同步工具的孤儿线程无法强杀
    tool_timeout_s: float = Field(default=10.0)
    # 失败后额外重试次数（不含首次）；入参校验失败不重试
    tool_max_retries: int = Field(default=1)
    # 输出截断：单个字符串字段最大长度，超出截断防止大块文本污染上下文
    tool_max_output_chars: int = Field(default=2000)
    # 输出截断：单个列表最多保留条数
    tool_max_list_items: int = Field(default=30)

    # MCP 客户端桥：单次跨进程调用的安全上限（秒）。注意这是「兜底上限」，
    # 真正的单工具超时由 base.Tool 的 timeout_s 控制（如 kb_search=60s）；
    # 这里设更宽松，避免抢在工具级超时之前误杀，也用于会话启动的等待上限。
    mcp_call_timeout_s: float = Field(default=90.0)

    # ---------- Postgres ----------
    postgres_host: str = Field(default="localhost")
    postgres_port: int = Field(default=5432)
    postgres_db: str = Field(default="opsagent")
    postgres_user: str = Field(default="opsagent")
    postgres_password: str = Field(default="")
    database_url: str = Field(default="")

    # ---------- Langfuse 可观测（优先本地，降级 Cloud）----------
    # 本地自托管（make langfuse-up 后自动可用）
    langfuse_local_host: str = Field(default="http://localhost:3000")
    langfuse_local_public_key: str = Field(default="pk-lf-local-opsagent")
    langfuse_local_secret_key: str = Field(default="sk-lf-local-opsagent")
    # Cloud 兜底（本地不可用时降级）
    langfuse_public_key: str = Field(default="")
    langfuse_secret_key: str = Field(default="")
    langfuse_host: str = Field(default="https://cloud.langfuse.com")

    # ---------- 数据合成 ----------
    synth_seed: int = Field(default=42)

    # ---------- W2 RAG 检索 ----------
    # embedding / rerank 走 FlagEmbedding 本地推理（不经 litellm）
    embedding_model: str = Field(default="BAAI/bge-large-zh-v1.5")
    embedding_dim: int = Field(default=1024)
    rerank_model: str = Field(default="BAAI/bge-reranker-v2-m3")
    # 两路召回各取 top_n，融合后送 rerank 的候选池上限
    retrieval_recall_top_n: int = Field(default=50)
    # RRF 融合常数 k（越小排名差异越锐利；60 过平滑导致正确文档被埋，降到 30）
    retrieval_rrf_k: int = Field(default=30)
    # rerank 阈值：低于此分数的文档视为噪声不返回（bge-reranker sigmoid 输出 0-1）
    retrieval_min_rerank_score: float = Field(default=0.35)
    # 是否启用 LLM query rewriting（桥接口语症状 ↔ 技术术语的语义鸿沟）
    retrieval_rewrite_enabled: bool = Field(default=True)
    # 知识库 chunk 表名
    kb_table: str = Field(default="kb_chunks")
    # bm25s 离线索引落盘目录（已 gitignore）
    bm25_index_dir: str = Field(default=str(PROJECT_ROOT / "bm25_index"))

    # ---------- W6 Multi-Agent ----------
    # 假设驱动并行诊断：Coordinator 生成假设数上限
    multi_agent_max_hypotheses: int = Field(default=3)
    # 单个 Worker 最多工具调用次数
    worker_max_tool_calls: int = Field(default=3)
    # 各节点模型别名（复用现有别名，可独立切换）
    model_coordinator: str = Field(default="plan")
    model_worker: str = Field(default="plan")
    model_synthesizer: str = Field(default="answer")
    # 图版本："v1"=ReAct 单 Agent, "v2"=Multi-Agent 假设驱动并行
    graph_version: str = Field(default="v2")

    # ---------- 数据资产路径（W1 工具读取用）----------
    # 合成日志目录：search_logs 读 fault_F*_claude.jsonl
    logs_dir: str = Field(default=str(PROJECT_ROOT / "data" / "logs" / "synth"))
    # 知识库目录：search_sop 关键词检索 sops/postmortems/runbooks/services
    docs_dir: str = Field(default=str(PROJECT_ROOT / "data" / "docs"))
    # Chainlit 前端连接后端 SSE 的基址（0.0.0.0 仅用于监听，连接走 localhost）
    api_base_url: str = Field(default="")

    @property
    def effective_api_base_url(self) -> str:
        """前端实际连接地址：未显式配置时用 localhost + app_port。"""
        if self.api_base_url:
            return self.api_base_url
        host = "127.0.0.1" if self.app_host in ("0.0.0.0", "") else self.app_host
        return f"http://{host}:{self.app_port}"

    @property
    def pg_dsn(self) -> str:
        """psycopg3 直连用的 libpq DSN。

        .env 里的 DATABASE_URL 是 SQLAlchemy 形式（postgresql+psycopg://...），
        psycopg.connect 不认 +psycopg 驱动后缀，这里剥掉；未配置则由 postgres_* 拼装。
        """
        if self.database_url:
            return self.database_url.replace("postgresql+psycopg://", "postgresql://", 1)
        return (
            f"postgresql://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    model_config = SettingsConfigDict(
        env_file=str(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """单例缓存，避免每次都重读 .env。"""
    return Settings()


# 业务代码统一通过这个对象访问配置
settings = get_settings()
