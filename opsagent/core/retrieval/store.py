"""pgvector 存取层 —— kb_chunks 表的建表 / 写入 / 向量召回。

用 psycopg3 直连（非 SQLAlchemy），配合 pgvector 的 psycopg 适配：
- register_vector 后可直接用 numpy 数组绑定 vector 列
- 向量召回用 `embedding <=> %s`（vector_cosine_ops + 归一化向量 = 余弦距离）

PG 在本架构里只承担"向量 ANN 索引"角色；chunk 正文/元数据由 pipeline 载入内存缓存，
不在每次召回时回表取大字段。
"""

from __future__ import annotations

from typing import Any

import numpy as np
import psycopg
from loguru import logger
from pgvector.psycopg import register_vector

from opsagent.core.config import settings
from opsagent.core.retrieval.chunker import Chunk

# 建表 + HNSW 余弦索引（幂等，符合 db_init.sql"业务表由模块按需建"的约定）
_DDL = """
CREATE TABLE IF NOT EXISTS {table} (
    id           BIGSERIAL PRIMARY KEY,
    doc_id       TEXT NOT NULL,
    title        TEXT NOT NULL,
    category     TEXT NOT NULL,
    section      TEXT,
    chunk_index  INT  NOT NULL,
    content      TEXT NOT NULL,
    source_lines TEXT,
    embedding    vector({dim}),
    created_at   TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (doc_id, chunk_index)
);
CREATE INDEX IF NOT EXISTS idx_{table}_embedding
    ON {table} USING hnsw (embedding vector_cosine_ops);
"""


def connect() -> psycopg.Connection:
    """开一条注册了 vector 类型的连接。"""
    conn = psycopg.connect(settings.pg_dsn)
    register_vector(conn)
    return conn


def ensure_schema(conn: psycopg.Connection) -> None:
    """幂等建表 + 索引。"""
    conn.execute(_DDL.format(table=settings.kb_table, dim=settings.embedding_dim))
    conn.commit()


def rebuild(chunks: list[Chunk], embeddings: np.ndarray) -> list[int]:
    """清表重灌全部 chunk，返回按插入顺序的 PG id 列表（供 bm25 索引对齐）。"""
    table = settings.kb_table
    ids: list[int] = []
    with connect() as conn:
        ensure_schema(conn)
        conn.execute(f"TRUNCATE {table} RESTART IDENTITY")
        with conn.cursor() as cur:
            for c, emb in zip(chunks, embeddings, strict=True):
                cur.execute(
                    f"INSERT INTO {table} "
                    "(doc_id, title, category, section, chunk_index, content, source_lines, embedding) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
                    (c.doc_id, c.title, c.category, c.section, c.chunk_index,
                     c.content, c.source_lines, emb),
                )
                ids.append(cur.fetchone()[0])
        conn.commit()
    logger.info(f"[store] 已写入 {len(ids)} 个 chunk 到 {table}")
    return ids


def vector_search(query_vec: np.ndarray, n: int) -> list[int]:
    """向量召回：返回余弦距离最近的 n 个 chunk id（按距离升序）。"""
    table = settings.kb_table
    with connect() as conn:
        rows = conn.execute(
            f"SELECT id FROM {table} ORDER BY embedding <=> %s LIMIT %s",
            (query_vec, n),
        ).fetchall()
    return [r[0] for r in rows]


def load_all_chunks() -> dict[int, dict[str, Any]]:
    """全量载入 chunk 元数据 + 正文（pipeline 缓存用），key 为 PG id。"""
    table = settings.kb_table
    out: dict[int, dict[str, Any]] = {}
    with connect() as conn:
        rows = conn.execute(
            f"SELECT id, doc_id, title, category, section, chunk_index, content, source_lines "
            f"FROM {table} ORDER BY id"
        ).fetchall()
    for r in rows:
        out[r[0]] = {
            "id": r[0], "doc_id": r[1], "title": r[2], "category": r[3],
            "section": r[4], "chunk_index": r[5], "content": r[6], "source_lines": r[7],
        }
    return out
