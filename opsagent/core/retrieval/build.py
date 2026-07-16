"""离线建索引 —— 一次性把知识库灌进 pgvector + 建 BM25 索引。

流程：chunk_all → bge 向量化 → 写 kb_chunks（含 HNSW 索引）→ 以 PG id 对齐建 BM25 索引存盘。

运行：`python -m opsagent.core.retrieval.build`（或 `make kb-index`）
前置：`make db-up` 起好 Postgres；首次会下载 bge-large-zh 模型（~1.3GB）。
注意：会 TRUNCATE 重灌 kb_chunks（幂等），命中数据库。
"""

from __future__ import annotations

from loguru import logger

from opsagent.core.config import settings
from opsagent.core.retrieval.bm25 import build_index
from opsagent.core.retrieval.chunker import chunk_all
from opsagent.core.retrieval.embedder import get_embedder
from opsagent.core.retrieval.store import rebuild


def main() -> None:
    chunks = chunk_all()
    if not chunks:
        logger.error("[build] 没有可入库的 chunk，检查 docs_dir")
        return

    texts = [c.content for c in chunks]
    logger.info(f"[build] 向量化 {len(texts)} 个 chunk ...")
    embeddings = get_embedder().embed_docs(texts)

    ids = rebuild(chunks, embeddings)
    build_index(texts, ids, settings.bm25_index_dir)

    logger.info(
        f"[build] 完成：{len(chunks)} chunk 入 {settings.kb_table} + BM25 索引 "
        f"({settings.bm25_index_dir})"
    )


if __name__ == "__main__":
    main()
