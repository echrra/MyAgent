"""把 search_sop 切换到 poisoned SOP 语料的机制。

实现思路（monkey patch,不动主代码）：
- 测试启动时,把 opsagent.core.tools.real.search_sop 替换为一个
  「只读 gate_harness_test/mock_data/sop_corpus_poisoned/ 里的 .md」的简易实现
- 测试跑完后恢复原 search_sop

为什么用 monkey patch 而不改主代码：
- 主代码不该感知到"gate 测试模式"的存在
- 不让"是否用毒化 SOP"成为生产环境的一个状态(避免哪天误开)

适用范围：
- 仅 P 系列(SOP 投毒)情景需要切
- 其他情景不需要调本模块

限制：
- 毒化 SOP 检索是简化实现（关键词子串匹配,不做向量/BM25/rerank),
  目的是保证毒文档能被命中,而不是模拟真实检索精度
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from loguru import logger

POISONED_SOP_DIR = Path(__file__).resolve().parent / "sop_corpus_poisoned"

_original_search_sop = None


def _parse_front_matter(content: str) -> dict[str, str]:
    """解析 .md 文件头部的 --- yaml front matter ---（仅取 doc_id 和 title）。"""
    match = re.match(r"^---\n(.*?)\n---\n(.*)$", content, re.DOTALL)
    if not match:
        return {}
    fm_text = match.group(1)
    meta: dict[str, str] = {}
    for line in fm_text.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            meta[k.strip()] = v.strip()
    return meta


def _poisoned_search_sop(query: str, top_k: int = 5) -> dict[str, Any]:
    """毒化 SOP 检索:只搜 gate_harness_test/mock_data/sop_corpus_poisoned/ 下的 .md。

    实现: 关键词子串匹配（标题 + 正文）,命中打分=命中次数。
    """
    if not POISONED_SOP_DIR.is_dir():
        logger.warning(f"[poisoned_sop] 目录不存在: {POISONED_SOP_DIR}")
        return {"data": [], "meta": {"query": query, "top_k": top_k, "mode": "poisoned"}}

    results = []
    # 简单分词:按空格/标点拆开,逐个匹配子串
    terms = [t for t in re.split(r"[\s,;.!?;:!?()\[\]{}<>\"'/-]+", query) if len(t) >= 2]
    for md_path in POISONED_SOP_DIR.glob("*.md"):
        content = md_path.read_text(encoding="utf-8")
        meta = _parse_front_matter(content)
        doc_id = meta.get("doc_id", md_path.stem)
        title = meta.get("title", md_path.stem)

        hit_count = sum(1 for t in terms if t in content)
        if hit_count == 0:
            continue
        results.append(
            {
                "doc_id": doc_id,
                "title": title,
                "snippet": content[:500],  # 前 500 字,够 LLM 看到毒步骤
                "content": content,
                "score": float(hit_count),
                "category": "sop_poisoned",
                "section": "",
                "source_lines": len(content.splitlines()),
            }
        )

    # 按命中数排序，截到 top_k
    results.sort(key=lambda x: -x["score"])
    results = results[:top_k]
    return {
        "data": results,
        "meta": {
            "query": query,
            "top_k": top_k,
            "mode": "poisoned",
            "recall_vec": 0,
            "recall_bm25": len(results),
            "reranked": False,
        },
    }


def switch_to_poisoned_sop() -> None:
    """把 search_sop 切到毒化语料。"""
    global _original_search_sop
    if _original_search_sop is not None:
        logger.warning("[poisoned_sop] 已经切换过,跳过")
        return

    from opsagent.core.tools import real
    from opsagent.core.tools import TOOL_REGISTRY

    _original_search_sop = real.search_sop
    real.search_sop = _poisoned_search_sop
    # 同步替换 TOOL_REGISTRY 里 search_sop 的 fn
    if "search_sop" in TOOL_REGISTRY:
        TOOL_REGISTRY["search_sop"].fn = _poisoned_search_sop
    logger.info("[poisoned_sop] search_sop 已切换到毒化语料")


def restore_normal_sop() -> None:
    """恢复原 search_sop。"""
    global _original_search_sop
    if _original_search_sop is None:
        return

    from opsagent.core.tools import real
    from opsagent.core.tools import TOOL_REGISTRY

    real.search_sop = _original_search_sop
    if "search_sop" in TOOL_REGISTRY:
        TOOL_REGISTRY["search_sop"].fn = _original_search_sop
    _original_search_sop = None
    logger.info("[poisoned_sop] search_sop 已恢复")
