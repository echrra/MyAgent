"""混合检索流水线 —— 向量召回 + BM25 → RRF 融合 → rerank 精排。

懒加载单例编排：chunk 元数据从 PG 全量载入内存缓存（~200 条），PG 仅承担向量 ANN；
BM25 索引、embedder、reranker 各自懒加载。供 search_sop 经 to_thread 同步调用。

P0 优化（W7 Badcase 攻坚）：
- query rewriting：LLM 把口语症状改写为检索关键词，桥接语义鸿沟
- rerank 阈值：过滤低分噪声文档
- RRF k=30：更锐利的融合排名
"""

from __future__ import annotations

from functools import lru_cache
from threading import Semaphore
from typing import Any

from loguru import logger

from opsagent.core.config import settings
from opsagent.core.retrieval.bm25 import get_bm25
from opsagent.core.retrieval.embedder import get_embedder
from opsagent.core.retrieval.fusion import rrf_fuse
from opsagent.core.retrieval.query_rewriter import rewrite_query
from opsagent.core.retrieval.reranker import get_reranker
from opsagent.core.retrieval.store import load_all_chunks, vector_search

# 限制并发 retrieve() 数量：embedding + reranker 是 CPU 密集型，多线程并发只会
# GIL 争抢、互相拖慢，反而把单次耗时从 ~20s 拉到 60-120s（触发工具超时返空 → cite=0）。
# 限 2 并发让每次调用独占 CPU 快速完成，把"无序卡死"变"有序排队"。
_RETRIEVE_SEMAPHORE = Semaphore(2)


@lru_cache(maxsize=1)
def _chunks() -> dict[int, dict[str, Any]]:
    """PG 全量 chunk 缓存（id → chunk dict）。"""
    chunks = load_all_chunks()
    logger.info(f"[pipeline] 载入 {len(chunks)} 个 chunk 元数据")
    return chunks


def retrieve(query: str, top_k: int = 5, use_rerank: bool = True) -> dict[str, Any]:
    """混合检索。

    Args:
        query: 查询
        top_k: 最终返回条数
        use_rerank: 是否走 reranker 精排（False 仅用 RRF 名次，供评测对照）

    Returns:
        {"results": [chunk dict + score...], "recall_vec", "recall_bm25", "reranked"}
    """
    chunks = _chunks()
    empty = {"results": [], "recall_vec": 0, "recall_bm25": 0, "reranked": False}
    if not chunks:
        return empty

    # 限并发：embedding + reranker 是 CPU 密集，多路并发只会 GIL 争抢、互相拖慢。
    # 信号量让最多 2 个 retrieve 同时跑，其余有序排队，避免单次耗时被拉到 60s+ 触发超时。
    with _RETRIEVE_SEMAPHORE:
        # 0) Query Rewriting：桥接口语症状 ↔ 技术术语的语义鸿沟
        search_query = rewrite_query(query)

        n = settings.retrieval_recall_top_n
        # 1) 向量召回
        qv = get_embedder().embed_query(search_query)
        vec_ids = vector_search(qv, n)
        # 2) BM25 召回（索引缺失则跳过，仅向量）
        bm = get_bm25()
        bm25_ids = bm.search(search_query, n) if bm else []
        # 3) RRF 融合，过滤掉不在缓存里的（理论上不会有）
        fused = [i for i in rrf_fuse([vec_ids, bm25_ids], settings.retrieval_rrf_k) if i in chunks]
        fused = fused[:n]
        if not fused:
            return {**empty, "recall_vec": len(vec_ids), "recall_bm25": len(bm25_ids)}

        # 4) 精排 or RRF 名次兜底
        # rerank 用改写后的 search_query（而非原始口语 query）：cross-encoder 对
        # 「口语症状 ↔ 技术术语 SOP 正文」这类不对称 pair 打分偏低，改写 query 把症状
        # 翻成技术词，与 chunk 文体更接近，rerank 命中率显著提升（实测验证）。
        if use_rerank:
            scores = get_reranker().compute_score([(search_query, chunks[i]["content"]) for i in fused])
            order = sorted(zip(fused, scores, strict=True), key=lambda x: x[1], reverse=True)
            reranked = True
        else:
            order = [(i, 1.0 / rank) for rank, i in enumerate(fused, start=1)]
            reranked = False

        # 5) 按 doc_id 去重 + rerank 阈值过滤
        min_score = settings.retrieval_min_rerank_score if use_rerank else 0.0
        results, seen = [], set()
        for i, s in order:
            if s < min_score:
                break  # 已按分数降序，后面更低直接跳出
            doc_id = chunks[i]["doc_id"]
            if doc_id in seen:
                continue
            seen.add(doc_id)
            results.append({**chunks[i], "score": round(float(s), 4)})
            if len(results) >= top_k:
                break
        return {
            "results": results,
            "recall_vec": len(vec_ids),
            "recall_bm25": len(bm25_ids),
            "reranked": reranked,
        }
