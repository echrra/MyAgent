# =============================================================
# OpsAgent Makefile
# 所有常用入口集中在此；新人 clone 下来照 README 跑 5 个目标即可
# =============================================================

# 加载 .env（如存在）到 make 上下文，方便 db-up 等读取变量
ifneq (,$(wildcard ./.env))
	include .env
	export
endif

.DEFAULT_GOAL := help

# ---------- 帮助 ----------
.PHONY: help
help:  ## 显示所有可用目标
	@echo "OpsAgent — 常用命令:"
	@awk 'BEGIN {FS = ":.*?## "} /^[a-zA-Z_-]+:.*?## / {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

# ---------- 环境 ----------
.PHONY: install
install:  ## 安装依赖（基于 uv，自动建 .venv）
	@which uv > /dev/null || (echo "请先安装 uv: curl -LsSf https://astral.sh/uv/install.sh | sh" && exit 1)
	uv sync --extra dev
	@echo "✅ 依赖安装完成。激活虚拟环境: source .venv/bin/activate"

.PHONY: env
env:  ## 若无 .env 则从 .env.example 拷贝
	@test -f .env || (cp .env.example .env && echo "✅ 已生成 .env，请填入 API Key")
	@test -f .env && echo "ℹ️  .env 已存在，未覆盖"

# ---------- 数据库 ----------
.PHONY: db-up
db-up:  ## 启动 Postgres + pgvector（后台）
	docker compose up -d postgres
	@echo "⏳ 等待数据库就绪..."
	@until docker compose exec -T postgres pg_isready -U $${POSTGRES_USER:-opsagent} > /dev/null 2>&1; do sleep 1; done
	@echo "✅ Postgres 已就绪 → localhost:$${POSTGRES_PORT:-5432}"

.PHONY: db-down
db-down:  ## 停止数据库（保留数据卷）
	docker compose down

.PHONY: db-reset
db-reset:  ## ⚠️ 销毁数据库 + 卷（不可逆，需确认）
	@echo "⚠️  即将删除 ./volumes/pg_data 内全部数据。"
	@read -p "输入 yes 继续: " ans && [ "$$ans" = "yes" ] || (echo "已取消" && exit 1)
	docker compose down -v
	rm -rf volumes/pg_data
	@echo "✅ 数据库已重置"

.PHONY: db-shell
db-shell:  ## 进入 psql 交互
	docker compose exec postgres psql -U $${POSTGRES_USER:-opsagent} -d $${POSTGRES_DB:-opsagent}

# ---------- 烟测 ----------
.PHONY: test-llm
test-llm:  ## 烟测 LLM API 是否通（.env 必须配好）
	uv run python scripts/test_llm.py

.PHONY: test-graph
test-graph:  ## Agent 图端到端烟测（Plan-Tool-Reflect 完整循环 + 真实工具）
	uv run python scripts/test_graph.py

.PHONY: test-sse
test-sse:  ## SSE 端到端烟测（需先 make api 起后端）
	uv run python scripts/test_sse.py

.PHONY: test-rag
test-rag:  ## 检索单测（chunker/RRF/分词 + 集成冒烟，DB 不通自动 skip）
	uv run pytest tests/test_retrieval.py -v

.PHONY: test-mcp
test-mcp:  ## MCP 双轨烟测（独立拉起 kb/metrics server，纯客户端 list+call 验证协议）
	uv run python scripts/test_mcp.py

# ---------- W2 知识库 RAG ----------
.PHONY: kb-index
kb-index:  ## 建知识库索引（chunk→bge向量→pgvector + BM25；需先 make db-up，会重灌 kb_chunks）
	uv run python -m opsagent.core.retrieval.build

.PHONY: eval-rag
eval-rag:  ## 检索评测：Recall@5 / MRR（rerank 开关对比；需先 make kb-index）
	uv run python scripts/eval_retrieval.py

# ---------- 数据合成（W0 后期补脚本）----------
.PHONY: data-real
data-real:  ## 下载 LogHub 真实日志（层 1，~3MB，无需登录）
	uv run python data/synthesizer/download_loghub.py

.PHONY: synth
synth:  ## 合成故障日志（W0：当前仅 F1；用 FAULT/WINDOWS/SEED 覆盖默认）
	uv run python -m data.synthesizer.run_all \
		--fault $${FAULT:-F1} \
		--windows $${WINDOWS:-2} \
		--seed $${SYNTH_SEED:-42}

# ---------- 应用 / Demo ----------
.PHONY: api
api:  ## 启动 FastAPI 后端（SSE 端点 /chat）
	uv run uvicorn opsagent.app.main:app --reload \
		--host $${APP_HOST:-0.0.0.0} --port $${APP_PORT:-8000}

.PHONY: demo
demo:  ## 启动 Chainlit 前端（端口 8001，避开后端 8000；需先 make api 起后端）
	@# 清空 DATABASE_URL：避免 Chainlit 自动启用自带持久化数据层（依赖 asyncpg）。
	@# 本项目的对话/记忆持久化由 W3 自研，不用 Chainlit 内置那套。
	DATABASE_URL= uv run chainlit run opsagent/ui/chainlit_app.py -w --port 8001

# ---------- 评测（W5）----------
.PHONY: eval
eval:  ## 跑全量评测（56 条单轮，需 DB + LLM 可用）
	uv run python -m eval.runner --dataset eval/dataset/cases

.PHONY: eval-quick
eval-quick:  ## 跑 quick subset（10 条，PR 级快速回归）
	uv run python -m eval.runner --quick

.PHONY: eval-multi
eval-multi:  ## 跑多轮对话评测（5 条，测试 episodic memory）
	uv run python -m eval.runner --multi-turn --timeout 240

.PHONY: eval-median
eval-median:  ## 全量评测 3 次取中位数（可靠 baseline，需 ~90min）
	uv run python -m eval.runner --concurrency 2 --timeout 210 --runs 3

# ---------- Langfuse 本地自托管（可观测平台）----------
.PHONY: langfuse-up
langfuse-up:  ## 启动本地 Langfuse（Web + Worker + PG + ClickHouse + Redis + MinIO）
	docker compose -f langfuse/docker-compose.yml --env-file langfuse/.env up -d
	@echo "⏳ 等待 Langfuse 就绪..."
	@timeout=60; while [ $$timeout -gt 0 ]; do \
		curl -sf http://localhost:3000/api/public/health > /dev/null 2>&1 && break; \
		sleep 2; timeout=$$((timeout - 2)); \
	done
	@curl -sf http://localhost:3000/api/public/health > /dev/null 2>&1 \
		&& echo "✅ Langfuse 已就绪 → http://localhost:3000" \
		&& echo "   账号: admin@local.dev / $${LANGFUSE_ADMIN_PASSWORD:-admin123}" \
		&& echo "   SDK Key: pk-lf-local-opsagent / sk-lf-local-opsagent" \
		|| echo "⚠️  Langfuse 尚未就绪，请稍后检查 docker compose -f langfuse/docker-compose.yml logs"

.PHONY: langfuse-down
langfuse-down:  ## 停止本地 Langfuse（保留数据卷）
	docker compose -f langfuse/docker-compose.yml --env-file langfuse/.env down
	@echo "✅ Langfuse 已停止（数据卷保留，重启不丢数据）"

.PHONY: langfuse-reset
langfuse-reset:  ## ⚠️ 销毁 Langfuse 全部数据（不可逆）
	@echo "⚠️  即将删除 Langfuse 全部数据卷（PG + ClickHouse + Redis + MinIO）"
	@read -p "输入 yes 继续: " ans && [ "$$ans" = "yes" ] || (echo "已取消" && exit 1)
	docker compose -f langfuse/docker-compose.yml --env-file langfuse/.env down -v
	@echo "✅ Langfuse 已重置"

.PHONY: langfuse-logs
langfuse-logs:  ## 查看 Langfuse 日志（实时跟踪）
	docker compose -f langfuse/docker-compose.yml --env-file langfuse/.env logs -f langfuse-web langfuse-worker

# ---------- 代码质量 ----------
.PHONY: lint
lint:  ## ruff 检查
	uv run ruff check opsagent eval data scripts tests

.PHONY: fmt
fmt:  ## ruff 自动格式化
	uv run ruff format opsagent eval data scripts tests

.PHONY: typecheck
typecheck:  ## mypy 类型检查
	uv run mypy opsagent

.PHONY: test
test:  ## 跑 pytest
	uv run pytest tests -v

.PHONY: check
check: lint typecheck test  ## 全套检查（CI 也跑这个）

# ---------- 清理 ----------
.PHONY: clean
clean:  ## 清理缓存文件
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name ".pytest_cache" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name ".ruff_cache" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name ".mypy_cache" -exec rm -rf {} + 2>/dev/null || true
	@echo "✅ 缓存清理完成"
