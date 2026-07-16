"""W2 检索评测 —— 跑评测集，算 Recall@5 / MRR / NDCG@5，对比 rerank 开/关。

跑法（需先 make db-up + make kb-index 建好索引）：
    uv run python scripts/eval_retrieval.py
    # 或 make eval-rag

指标说明：
- Recall@5：top-5 内是否命中任一 gold（rel>0），看「找没找到」
- MRR：首个命中的名次倒数，看「排得多靠前」
- NDCG@5：分级相关度下的折损累计增益，看「整体排序质量」—— 对 rerank 最敏感
  rerank 的价值主要体现在把 rel=2 的核心文档从召回中段拉到 top1，
  Recall 可能不变，但 MRR / NDCG 会涨。
"""

import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import yaml

from opsagent.core.retrieval.pipeline import retrieve

_QUERIES = Path(__file__).resolve().parents[1] / "eval" / "retrieval" / "queries.yaml"
TOP_K = 5


def _load_cases() -> list[dict]:
    data = yaml.safe_load(_QUERIES.read_text(encoding="utf-8"))
    return data["queries"]


def _first_hit_rank(doc_ids: list[str], gold: dict[str, int]) -> int:
    """返回首个命中 gold（rel>0）的 1 基名次；未命中返回 0。"""
    for rank, did in enumerate(doc_ids, start=1):
        if gold.get(did, 0) > 0:
            return rank
    return 0


def _dcg(rels: list[int]) -> float:
    """折损累计增益：增益用 2^rel-1，位置折损 1/log2(rank+1)。"""
    return sum((2**rel - 1) / math.log2(rank + 1) for rank, rel in enumerate(rels, start=1))


def _ndcg(doc_ids: list[str], gold: dict[str, int], k: int) -> float:
    """NDCG@k：实际排序 DCG / 理想排序 IDCG。"""
    rels = [gold.get(did, 0) for did in doc_ids[:k]]
    ideal = sorted(gold.values(), reverse=True)[:k]
    idcg = _dcg(ideal)
    return _dcg(rels) / idcg if idcg > 0 else 0.0


def _evaluate(cases: list[dict], use_rerank: bool) -> tuple[float, float, float, list[dict]]:
    """跑一遍评测集，返回 (Recall@5, MRR, NDCG@5, 逐条明细)。"""
    hits, rr_sum, ndcg_sum = 0, 0.0, 0.0
    rows = []
    for c in cases:
        gold = c["gold"]
        out = retrieve(c["query"], top_k=TOP_K, use_rerank=use_rerank)
        doc_ids = [r["doc_id"] for r in out["results"]]
        rank = _first_hit_rank(doc_ids, gold)
        ndcg = _ndcg(doc_ids, gold, TOP_K)
        hits += int(rank > 0)
        rr_sum += (1.0 / rank) if rank else 0.0
        ndcg_sum += ndcg
        rows.append({"query": c["query"], "doc_ids": doc_ids, "rank": rank, "ndcg": ndcg})
    n = len(cases)
    return hits / n, rr_sum / n, ndcg_sum / n, rows


def main() -> None:
    cases = _load_cases()
    print(f"评测集：{len(cases)} 条对抗性 query，Top-{TOP_K} 命中 + 分级相关度\n")

    recall_off, mrr_off, ndcg_off, rows_off = _evaluate(cases, use_rerank=False)
    recall_on, mrr_on, ndcg_on, rows_on = _evaluate(cases, use_rerank=True)

    # 逐条对比：同一 query rerank 前后的首命中名次，直观看精排把 gold 拉前了多少
    print("逐条明细（名次：OFF → ON，↑ 表示 rerank 把命中拉前）:")
    for ro, rn in zip(rows_off, rows_on, strict=True):
        po = f"@{ro['rank']}" if ro["rank"] else "miss"
        pn = f"@{rn['rank']}" if rn["rank"] else "miss"
        arrow = "↑" if (rn["rank"] and (not ro["rank"] or rn["rank"] < ro["rank"])) else " "
        print(f"  {arrow} {po:>4} → {pn:>4}  ndcg {ro['ndcg']:.2f}→{rn['ndcg']:.2f}  {rn['query'][:30]}")
        print(f"            ON top5: {rn['doc_ids']}")

    print("\n========== 汇总（rerank on vs off）==========")
    print(f"{'指标':<12}{'rerank OFF':>14}{'rerank ON':>14}{'提升':>10}")
    print(f"{'Recall@5':<12}{recall_off:>14.1%}{recall_on:>14.1%}{recall_on - recall_off:>+10.1%}")
    print(f"{'MRR':<12}{mrr_off:>14.3f}{mrr_on:>14.3f}{mrr_on - mrr_off:>+10.3f}")
    print(f"{'NDCG@5':<12}{ndcg_off:>14.3f}{ndcg_on:>14.3f}{ndcg_on - ndcg_off:>+10.3f}")

    target = 0.8
    verdict = "✅ 达标" if recall_on >= target else "❌ 未达标"
    print(f"\nW2 验收（Recall@5 > {target:.0%}）：{verdict}")
    gain = (mrr_on - mrr_off) > 0 or (ndcg_on - ndcg_off) > 0
    print(f"rerank 增量：{'✅ MRR/NDCG 有提升，精排有效' if gain else '⚠️ 无提升，评测集仍偏简单'}")


if __name__ == "__main__":
    main()
