"""W2 检索单测。

分两类：
- 确定性单元（不依赖 DB / 模型）：chunker 切分、RRF 融合、jieba 分词 —— CI 必跑
- 集成冒烟：依赖 PG + 已建索引 + bge 模型，连不上则 skip（本地建好索引后手动验）
"""

import pytest

from opsagent.core.retrieval.bm25 import tokenize
from opsagent.core.retrieval.chunker import chunk_all
from opsagent.core.retrieval.fusion import rrf_fuse

# ---------------- 确定性单元 ----------------


def test_chunk_all_covers_corpus():
    """全量切分：chunk 数量合理，34 篇文档全覆盖。"""
    chunks = chunk_all()
    assert len(chunks) > 100, "chunk 数量异常偏少"
    doc_ids = {c.doc_id for c in chunks}
    assert len(doc_ids) == 34, f"应覆盖 34 篇，实际 {len(doc_ids)}"


def test_chunk_metadata_complete():
    """每个 chunk 元数据齐全、正文非空、chunk_index 自增。"""
    chunks = chunk_all()
    for c in chunks:
        assert c.doc_id and c.title and c.category and c.content
        assert c.section
        assert "-" in c.source_lines  # "start-end"
    # 同一文档内 chunk_index 从 0 连续
    from collections import defaultdict

    per_doc = defaultdict(list)
    for c in chunks:
        per_doc[c.doc_id].append(c.chunk_index)
    for doc_id, idxs in per_doc.items():
        assert idxs == list(range(len(idxs))), f"{doc_id} chunk_index 不连续"


def test_chunk_content_has_title_prefix():
    """chunk 正文带 [title] 前缀，给向量补上下文。"""
    chunks = chunk_all()
    assert all(c.content.startswith(f"[{c.title}]") for c in chunks)


def test_rrf_fuse_ranking():
    """RRF：两路都靠前的 id 应融合后居首。"""
    # id=1 在两路都靠前，id=3 在第二路第一、第一路第三
    fused = rrf_fuse([[1, 2, 3], [3, 1, 4]], k=60)
    assert fused[0] == 1
    assert set(fused) == {1, 2, 3, 4}
    assert fused.index(3) < fused.index(2)  # 3 被两路加权，应在 2 之前


def test_rrf_empty_list():
    """空召回不报错。"""
    assert rrf_fuse([[], []], k=60) == []


def test_tokenize_chinese_and_code():
    """jieba 切中文 + 保留英文/代码 token，滤掉纯标点。"""
    toks = tokenize("edgectl-backend-http DNS 解析失败，no such host！")
    assert "dns" in toks  # 小写归一
    assert "解析" in toks
    assert "host" in toks
    assert "，" not in toks and "！" not in toks  # 纯标点被滤


# ---------------- 集成冒烟（需 DB + 索引 + 模型）----------------


@pytest.fixture
def kb_ready():
    """检查 PG 可连且 kb_chunks 有数据，否则 skip。"""
    try:
        from opsagent.core.config import settings
        from opsagent.core.retrieval.store import connect

        with connect() as conn:
            n = conn.execute(f"SELECT count(*) FROM {settings.kb_table}").fetchone()[0]
        if not n:
            pytest.skip("kb_chunks 为空，请先 make kb-index")
        return n
    except Exception as exc:
        pytest.skip(f"PG / 索引不可用，跳过集成测试: {exc}")


def test_retrieve_smoke(kb_ready):
    """端到端检索：DNS 故障 query 应在 top-5 命中 sop-f6。"""
    from opsagent.core.retrieval.pipeline import retrieve

    out = retrieve("域名解析失败 no such host 怎么排查", top_k=5)
    doc_ids = [r["doc_id"] for r in out["results"]]
    assert out["results"], "检索结果为空"
    assert any("f6" in d or "dns" in d or "coredns" in d for d in doc_ids), doc_ids
