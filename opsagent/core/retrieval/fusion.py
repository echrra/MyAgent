"""RRF（Reciprocal Rank Fusion）—— 手写多路召回融合。

把"向量召回"和"BM25 召回"两条 ranked 列表按排名融合：
    score(d) = Σ_i 1 / (k + rank_i(d))
- rank 从 1 起；某路未召回该 doc 则该项不计入
- k 越大，高低排名差异越平滑（业界惯例 k=60）

RRF 只用排名不用原始分，天然规避"向量余弦"与"BM25 分"量纲不可比的问题，
比加权求和更鲁棒——这是选它而非 weighted-sum 的核心理由。
"""

from __future__ import annotations


def rrf_fuse(ranked_lists: list[list[int]], k: int = 60) -> list[int]:
    """融合多路 ranked id 列表，返回按 RRF 分降序的 id 列表。

    Args:
        ranked_lists: 每路一个按相关性降序排好的 id 列表
        k: RRF 平滑常数
    """
    scores: dict[int, float] = {}
    for lst in ranked_lists:
        for rank, doc_id in enumerate(lst, start=1):
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank)
    return sorted(scores, key=lambda d: scores[d], reverse=True)
