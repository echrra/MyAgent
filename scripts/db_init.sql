-- =============================================================
-- OpsAgent 数据库初始化脚本
-- docker-compose 首次启动 Postgres 时自动执行（位于 initdb.d/）
-- 后续 schema 演进走 migration（W4 引入 alembic）
-- =============================================================

-- 1. 启用 pgvector 扩展（向量检索基础）
CREATE EXTENSION IF NOT EXISTS vector;

-- 2. 启用 pg_trgm（中文/英文模糊匹配辅助，部分查询会用）
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- 3. 健康检查表（验证 docker-compose 起来后 SQL 生效）
CREATE TABLE IF NOT EXISTS _opsagent_health (
    id          SERIAL PRIMARY KEY,
    note        TEXT,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

INSERT INTO _opsagent_health (note) VALUES ('opsagent db init ok');

-- 备注：
-- 真正的业务表（episodic_messages / profile / kb_chunks 等）
-- 由各模块的 W2/W4 任务按需创建，本文件不预创建，避免与代码不同步。
