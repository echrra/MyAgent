"""精排器 —— bge-reranker-v2-m3 本地推理（transformers 直跑）。

Cross-Encoder 把 (query, passage) 成对喂入，直接打相关性分，比双塔向量更准但更慢，
故只对融合后的候选池（top~50）做精排，取 top_k。懒加载单例。

实现说明：bge-reranker 本质是 XLM-RoBERTa 的序列分类头，按 BAAI model card 用
transformers 的 AutoModelForSequenceClassification 直跑。不走 FlagEmbedding 的
FlagReranker —— 后者在当前 transformers 版本下会把慢速 tokenizer 误走
prepare_for_model 路径而崩（XLMRobertaTokenizer has no attribute prepare_for_model）。

单例：不用 @lru_cache 而用手工 double-checked locking。原因是 LangGraph Send API
派多个 worker 并发调 search_sop 时，多线程同时命中 cache-miss 会各自触发
Reranker(...) 构造（模型权重 ~1GB 加载 3-5s）。lru_cache 对已返回结果线程安全，
但对首次并发进入无原子性保证。手工双检确保只加载一次。
"""

from __future__ import annotations

import threading

from loguru import logger

from opsagent.core.config import settings

# 单对截断长度：query + passage 拼接后的最大 token 数
_MAX_LEN = 512


class Reranker:
    """bge-reranker 包装（transformers 直跑，sigmoid 归一到 0-1）。"""

    def __init__(self, model_name: str):
        import torch
        from transformers import AutoModelForSequenceClassification, AutoTokenizer

        logger.info(f"[reranker] 加载模型 {model_name} ...")
        self._torch = torch
        self._tokenizer = AutoTokenizer.from_pretrained(model_name)
        self._model = AutoModelForSequenceClassification.from_pretrained(model_name)
        self._model.eval()
        logger.info("[reranker] 模型就绪")

    def compute_score(self, pairs: list[tuple[str, str]]) -> list[float]:
        """对 [(query, passage), ...] 打分，返回与输入等长的分数列表（0-1）。"""
        if not pairs:
            return []
        torch = self._torch
        with torch.no_grad():
            inputs = self._tokenizer(
                [list(p) for p in pairs],
                padding=True,
                truncation=True,
                max_length=_MAX_LEN,
                return_tensors="pt",
            )
            logits = self._model(**inputs, return_dict=True).logits.view(-1).float()
            scores = torch.sigmoid(logits)
        return [float(s) for s in scores.tolist()]


# --- 手工 double-checked locking（防多 worker 冷启动并发重复加载）---
_reranker_lock = threading.Lock()
_reranker_singleton: Reranker | None = None


def get_reranker() -> Reranker:
    """进程级单例。快路径无锁；冷启动串行化。"""
    global _reranker_singleton
    if _reranker_singleton is not None:
        return _reranker_singleton
    with _reranker_lock:
        if _reranker_singleton is None:
            _reranker_singleton = Reranker(settings.rerank_model)
    return _reranker_singleton

