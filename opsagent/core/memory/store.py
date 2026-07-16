"""记忆系统存取层 —— 4 层记忆中 L2/L3 的纯 SQL 读写（无业务语义）。

设计取舍：
- 复用 retrieval/store.py 的 connect()（已 register_vector），与 kb_chunks 共用同一 PG 实例，
  靠表名隔离，不另开连接池、不引 SQLAlchemy（与现有 W2 检索层保持一致）。
- 本层只负责「能连上时怎么读写」，DB 不可用的降级由各 memory 业务模块 try/except 兜底，
  本层不吞异常（让上层能记日志 + 优雅退回单轮）。
- 四张表对应 docs/04 的 L2（profile / profile_fault_patterns）与 L3（episodic_turns /
  episodic_summaries）；L1 在内存拼装、L4 在 State 里，均不落本层。

表名作为本模块结构常量（与 DDL 同处定义），不进 config —— config 里只放「阈值类」可调值。
"""

from __future__ import annotations

from typing import Any

import numpy as np
import psycopg

from opsagent.core.config import settings

# ---------- 表名（结构常量）----------
TBL_PROFILE = "profile"
TBL_FAULT_PATTERNS = "profile_fault_patterns"
TBL_EPISODIC_TURNS = "episodic_turns"
TBL_EPISODIC_SUMMARIES = "episodic_summaries"

# ---------- 建表 DDL（幂等，对齐 kb_chunks 风格：HNSW + vector_cosine_ops）----------
# L2 结构化画像：多版本 + 时间戳，不覆盖 —— 记忆冲突可见（docs/04 §5）
_DDL_PROFILE = f"""
CREATE TABLE IF NOT EXISTS {TBL_PROFILE} (
    id          BIGSERIAL PRIMARY KEY,
    user_id     TEXT NOT NULL,
    field       TEXT NOT NULL,
    value       TEXT NOT NULL,
    source      TEXT NOT NULL DEFAULT 'updater',
    created_at  TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_{TBL_PROFILE}_user_field
    ON {TBL_PROFILE} (user_id, field, created_at DESC);
"""

# L2 历史故障模式：向量检索，按 user_id 隔离
_DDL_FAULT_PATTERNS = """
CREATE TABLE IF NOT EXISTS {table} (
    id          BIGSERIAL PRIMARY KEY,
    user_id     TEXT NOT NULL,
    summary     TEXT NOT NULL,
    embedding   vector({dim}),
    created_at  TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_{table}_embedding
    ON {table} USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS idx_{table}_user
    ON {table} (user_id);
"""

# L3 对话原始历史：一轮两行（user / assistant 各一行），贴近 OpenAI messages 语义
_DDL_EPISODIC_TURNS = f"""
CREATE TABLE IF NOT EXISTS {TBL_EPISODIC_TURNS} (
    id          BIGSERIAL PRIMARY KEY,
    session_id  TEXT NOT NULL,
    turn        INT  NOT NULL,
    role        TEXT NOT NULL,
    content     TEXT NOT NULL,
    tool_calls  TEXT,
    created_at  TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (session_id, turn, role)
);
CREATE INDEX IF NOT EXISTS idx_{TBL_EPISODIC_TURNS}_session
    ON {TBL_EPISODIC_TURNS} (session_id, turn);
"""

# L3 压缩摘要：含递归层级（level），被合并的旧摘要置 superseded 不删
_DDL_EPISODIC_SUMMARIES = f"""
CREATE TABLE IF NOT EXISTS {TBL_EPISODIC_SUMMARIES} (
    id           BIGSERIAL PRIMARY KEY,
    session_id   TEXT NOT NULL,
    level        INT  NOT NULL DEFAULT 1,
    covers_start INT  NOT NULL,
    covers_end   INT  NOT NULL,
    summary      TEXT NOT NULL,
    superseded   BOOLEAN NOT NULL DEFAULT FALSE,
    created_at   TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_{TBL_EPISODIC_SUMMARIES}_session
    ON {TBL_EPISODIC_SUMMARIES} (session_id, level, covers_start);
"""


def ensure_memory_schema(conn: psycopg.Connection) -> None:
    """幂等建 4 张表 + 索引（对齐 db_init.sql「业务表由模块按需建」约定）。"""
    conn.execute(_DDL_PROFILE)
    conn.execute(
        _DDL_FAULT_PATTERNS.format(table=TBL_FAULT_PATTERNS, dim=settings.embedding_dim)
    )
    conn.execute(_DDL_EPISODIC_TURNS)
    conn.execute(_DDL_EPISODIC_SUMMARIES)
    conn.commit()


# ====================== L2 Profile：结构化字段 ======================

def upsert_profile_field(
    conn: psycopg.Connection, user_id: str, field: str, value: str, source: str = "updater"
) -> None:
    """插一条新版本（不覆盖）——多版本 + 时间戳，让冲突在上下文里可见。"""
    conn.execute(
        f"INSERT INTO {TBL_PROFILE} (user_id, field, value, source) VALUES (%s, %s, %s, %s)",
        (user_id, field, value, source),
    )
    conn.commit()


def load_profile_versions(conn: psycopg.Connection, user_id: str) -> list[dict[str, Any]]:
    """拉该 user 全部字段全部版本，按 (field, created_at desc)，最新优先。"""
    rows = conn.execute(
        f"SELECT field, value, source, created_at FROM {TBL_PROFILE} "
        "WHERE user_id = %s ORDER BY field ASC, created_at DESC",
        (user_id,),
    ).fetchall()
    return [
        {"field": r[0], "value": r[1], "source": r[2], "created_at": r[3]} for r in rows
    ]


# ====================== L2 Profile：历史故障模式（向量）======================

def insert_fault_pattern(
    conn: psycopg.Connection, user_id: str, summary: str, embedding: np.ndarray
) -> int:
    """写一条历史故障模式，返回 PG id。"""
    row = conn.execute(
        f"INSERT INTO {TBL_FAULT_PATTERNS} (user_id, summary, embedding) "
        "VALUES (%s, %s, %s) RETURNING id",
        (user_id, summary, embedding),
    ).fetchone()
    conn.commit()
    return row[0]


def search_fault_patterns(
    conn: psycopg.Connection, user_id: str, query_vec: np.ndarray, n: int
) -> list[dict[str, Any]]:
    """按 user_id 过滤 + 向量召回 Top-N（余弦距离升序）。"""
    rows = conn.execute(
        f"SELECT summary, embedding <=> %s AS dist FROM {TBL_FAULT_PATTERNS} "
        "WHERE user_id = %s ORDER BY embedding <=> %s LIMIT %s",
        (query_vec, user_id, query_vec, n),
    ).fetchall()
    return [{"summary": r[0], "dist": float(r[1])} for r in rows]


# ====================== L3 Episodic：对话原始历史 ======================

def append_turn(
    conn: psycopg.Connection,
    session_id: str,
    turn: int,
    role: str,
    content: str,
    tool_calls_json: str | None = None,
) -> None:
    """追加一行对话（一轮两行：user / assistant 各调一次）。

    UNIQUE(session_id, turn, role) 冲突时覆盖内容（同轮重试不重复堆积）。
    """
    conn.execute(
        f"INSERT INTO {TBL_EPISODIC_TURNS} (session_id, turn, role, content, tool_calls) "
        "VALUES (%s, %s, %s, %s, %s) "
        "ON CONFLICT (session_id, turn, role) DO UPDATE "
        "SET content = EXCLUDED.content, tool_calls = EXCLUDED.tool_calls",
        (session_id, turn, role, content, tool_calls_json),
    )
    conn.commit()


def load_recent_turns(
    conn: psycopg.Connection, session_id: str, limit_turns: int
) -> list[dict[str, Any]]:
    """取最近 N 轮的全部行，按 (turn, role) 正序返回（user 在 assistant 前）。

    limit 单位是「轮」不是「行」：先定位最近 limit_turns 个 turn 号，再取这些 turn 的所有行。
    """
    rows = conn.execute(
        f"SELECT turn, role, content, tool_calls FROM {TBL_EPISODIC_TURNS} "
        "WHERE session_id = %s AND turn > ("
        f"    SELECT COALESCE(MAX(turn), 0) - %s FROM {TBL_EPISODIC_TURNS} WHERE session_id = %s"
        ") "
        "ORDER BY turn ASC, CASE role WHEN 'user' THEN 0 ELSE 1 END ASC",
        (session_id, limit_turns, session_id),
    ).fetchall()
    return [
        {"turn": r[0], "role": r[1], "content": r[2], "tool_calls": r[3]} for r in rows
    ]


def load_turns_range(
    conn: psycopg.Connection, session_id: str, start_turn: int, end_turn: int
) -> list[dict[str, Any]]:
    """取 [start_turn, end_turn] 区间的全部行（供压缩取待压缩原文）。"""
    rows = conn.execute(
        f"SELECT turn, role, content, tool_calls FROM {TBL_EPISODIC_TURNS} "
        "WHERE session_id = %s AND turn BETWEEN %s AND %s "
        "ORDER BY turn ASC, CASE role WHEN 'user' THEN 0 ELSE 1 END ASC",
        (session_id, start_turn, end_turn),
    ).fetchall()
    return [
        {"turn": r[0], "role": r[1], "content": r[2], "tool_calls": r[3]} for r in rows
    ]


def count_turns(conn: psycopg.Connection, session_id: str) -> int:
    """会话累计轮数（distinct turn 号）。"""
    row = conn.execute(
        f"SELECT COUNT(DISTINCT turn) FROM {TBL_EPISODIC_TURNS} WHERE session_id = %s",
        (session_id,),
    ).fetchone()
    return int(row[0]) if row else 0


def max_turn(conn: psycopg.Connection, session_id: str) -> int:
    """当前最大 turn 号（无记录返回 0）。"""
    row = conn.execute(
        f"SELECT COALESCE(MAX(turn), 0) FROM {TBL_EPISODIC_TURNS} WHERE session_id = %s",
        (session_id,),
    ).fetchone()
    return int(row[0]) if row else 0


# ====================== L3 Episodic：压缩摘要 ======================

def insert_summary(
    conn: psycopg.Connection,
    session_id: str,
    level: int,
    covers_start: int,
    covers_end: int,
    summary_json: str,
) -> int:
    """写一条结构化摘要，返回 PG id。level=1 一级摘要；level≥2 递归合并产物。"""
    row = conn.execute(
        f"INSERT INTO {TBL_EPISODIC_SUMMARIES} "
        "(session_id, level, covers_start, covers_end, summary) "
        "VALUES (%s, %s, %s, %s, %s) RETURNING id",
        (session_id, level, covers_start, covers_end, summary_json),
    ).fetchone()
    conn.commit()
    return row[0]


def load_summaries(
    conn: psycopg.Connection, session_id: str, include_superseded: bool = False
) -> list[dict[str, Any]]:
    """按 (level asc, covers_start asc) 拉摘要。默认只取未被合并的（装配用）。"""
    sql = (
        f"SELECT id, level, covers_start, covers_end, summary, superseded "
        f"FROM {TBL_EPISODIC_SUMMARIES} WHERE session_id = %s"
    )
    if not include_superseded:
        sql += " AND superseded = FALSE"
    sql += " ORDER BY level ASC, covers_start ASC"
    rows = conn.execute(sql, (session_id,)).fetchall()
    return [
        {
            "id": r[0],
            "level": r[1],
            "covers_start": r[2],
            "covers_end": r[3],
            "summary": r[4],
            "superseded": r[5],
        }
        for r in rows
    ]


def mark_summaries_superseded(conn: psycopg.Connection, summary_ids: list[int]) -> None:
    """递归压缩后把被合并的低层摘要标记 superseded（不删，留复盘 + 讲「原始不丢」）。"""
    if not summary_ids:
        return
    conn.execute(
        f"UPDATE {TBL_EPISODIC_SUMMARIES} SET superseded = TRUE WHERE id = ANY(%s)",
        (summary_ids,),
    )
    conn.commit()
