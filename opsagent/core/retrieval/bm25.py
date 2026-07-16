"""BM25 稀疏检索 —— jieba 中文分词 + bm25s 索引。

为什么要自定义分词：bm25s 默认按空格切词，对中文正文无效。中文用 jieba 搜索引擎模式
切细粒度词；同时保留英文/代码 token（服务名 edgectl-backend-http、错误码、
`context deadline exceeded` 这类精确术语，BM25 对它们的召回最有价值）。

索引离线建好存盘（bm25_index/），运行时载入；id 数组与 PG id 对齐，融合按 id 进行。
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

import bm25s
import jieba
from loguru import logger

from opsagent.core.config import settings

_IDS_FILE = "chunk_ids.json"
# 至少含一个中文/字母/数字才保留（滤掉纯标点、空白 token）
_KEEP = re.compile(r"[\w一-鿿]")


def tokenize(text: str) -> list[str]:
    """jieba 搜索引擎模式切词 + 小写归一 + 滤纯标点。

    index 与 query 走同一函数，保证词表一致。
    """
    toks = []
    for t in jieba.lcut_for_search(text.lower()):
        t = t.strip()
        if t and _KEEP.search(t):
            toks.append(t)
    return toks


def build_index(corpus_texts: list[str], ids: list[int], out_dir: str) -> None:
    """对全量 chunk 文本建 BM25 索引并存盘，同时落 id 映射。"""
    corpus_tokens = [tokenize(t) for t in corpus_texts]
    retriever = bm25s.BM25()
    retriever.index(corpus_tokens)

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    retriever.save(str(out))
    (out / _IDS_FILE).write_text(json.dumps(ids, ensure_ascii=False), encoding="utf-8")
    logger.info(f"[bm25] 索引已存 {out}（{len(ids)} 个 chunk）")


class Bm25Index:
    """已加载的 BM25 索引，search 返回 PG chunk id。"""

    def __init__(self, retriever: bm25s.BM25, ids: list[int]):
        self._retriever = retriever
        self._ids = ids

    def search(self, query: str, n: int) -> list[int]:
        """返回 BM25 得分最高的 n 个 chunk id（按得分降序）。"""
        tokens = tokenize(query)
        if not tokens:
            return []
        k = min(n, len(self._ids))
        results, _ = self._retriever.retrieve([tokens], k=k, show_progress=False)
        # results 形状 (1, k)，元素是语料内位置下标 → 映射回 PG id
        return [self._ids[int(pos)] for pos in results[0]]


@lru_cache(maxsize=1)
def get_bm25() -> Bm25Index | None:
    """进程级单例；索引未建好则返回 None（上层降级处理）。"""
    out = Path(settings.bm25_index_dir)
    ids_path = out / _IDS_FILE
    if not ids_path.exists():
        logger.warning(f"[bm25] 索引不存在: {out}（请先 make kb-index）")
        return None
    retriever = bm25s.BM25.load(str(out), load_corpus=False)
    ids = json.loads(ids_path.read_text(encoding="utf-8"))
    logger.info(f"[bm25] 已载入索引（{len(ids)} 个 chunk）")
    return Bm25Index(retriever, ids)
