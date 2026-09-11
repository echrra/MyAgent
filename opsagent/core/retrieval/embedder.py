"""向量化器 —— bge-large-zh-v1.5 本地推理（FlagEmbedding）。

懒加载单例：首次调用才载入模型（CPU ~10-30s），之后复用。
- 文档向量用 encode（不加指令）
- 查询向量用 encode_queries（自动加检索指令，bge 系列要求 query/passage 不对称编码）
- 默认归一化，配合 pgvector 的 vector_cosine_ops 用 `<=>` 即余弦距离

单例：见 reranker.py 说明；同样用手工 double-checked locking 防并发首次加载。
"""

from __future__ import annotations

import threading

import numpy as np
from loguru import logger

from opsagent.core.config import settings

# bge-zh 官方推荐的检索指令前缀（拼在 query 前，passage 不加）
_QUERY_INSTRUCTION = "为这个句子生成表示用于检索相关文章："


class Embedder:
    """bge embedding 模型包装。"""

    def __init__(self, model_name: str):
        # 延迟到这里 import，避免没装 torch 的环境 import 本模块即报错
        import torch
        from FlagEmbedding import FlagModel

        # CUDA 可用时上 GPU；仅 CPU 环境保持默认 fp32 路径
        device = "cuda" if torch.cuda.is_available() else "cpu"
        logger.info(f"[embedder] 加载模型 {model_name} → {device}")
        self._model = FlagModel(
            model_name,
            query_instruction_for_retrieval=_QUERY_INSTRUCTION,
            use_fp16=(device == "cuda"),
            devices=device,  # FlagEmbedding 的参数名是复数 devices
        )
        logger.info(f"[embedder] 模型就绪 ({device})")

    def embed_docs(self, texts: list[str]) -> np.ndarray:
        """批量编码文档，返回 (n, dim) 归一化向量。"""
        vecs = np.asarray(self._model.encode(texts), dtype=np.float32)
        # NaN 防护：极端输入下置 0（PG vector 类型拒绝 NaN）
        return np.nan_to_num(vecs, nan=0.0, posinf=1.0, neginf=-1.0)

    def embed_query(self, query: str) -> np.ndarray:
        """编码单条查询（自动加检索指令），返回 (dim,) 向量。"""
        vecs = np.asarray(self._model.encode_queries([query]), dtype=np.float32)
        vecs = np.nan_to_num(vecs, nan=0.0, posinf=1.0, neginf=-1.0)
        return vecs[0]


# --- 手工 double-checked locking（防多 worker 冷启动并发重复加载）---
_embedder_lock = threading.Lock()
_embedder_singleton: Embedder | None = None


def get_embedder() -> Embedder:
    """进程级单例。快路径无锁；冷启动串行化。"""
    global _embedder_singleton
    if _embedder_singleton is not None:
        return _embedder_singleton
    with _embedder_lock:
        if _embedder_singleton is None:
            _embedder_singleton = Embedder(settings.embedding_model)
    return _embedder_singleton
