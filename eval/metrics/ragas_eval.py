"""Ragas 双指标包装 —— faithfulness + context_recall。

设计：
- 仅跑 docs/06 明确的两个指标
- 失败时返回 None（降级不崩）
- Runner 中通过 --ragas flag 开启
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from loguru import logger


@dataclass
class RagasResult:
    """Ragas 评测结果。"""

    faithfulness: float | None = None
    context_recall: float | None = None


def evaluate_ragas(
    query: str,
    answer: str,
    contexts: list[str],
    ground_truth: str = "",
) -> RagasResult | None:
    """跑 Ragas faithfulness + context_recall。

    Args:
        query: 用户问题
        answer: Agent 最终回答
        contexts: 从 working_memory 中提取的检索文本
        ground_truth: 期望答案（context_recall 需要）

    Returns:
        RagasResult 或 None（失败降级）
    """
    try:
        from datasets import Dataset
        from ragas import evaluate
        from ragas.metrics import context_recall, faithfulness

        data = {
            "question": [query],
            "answer": [answer],
            "contexts": [contexts],
        }
        metrics = [faithfulness]

        # context_recall 需要 ground_truth
        if ground_truth:
            data["ground_truth"] = [ground_truth]
            metrics.append(context_recall)

        ds = Dataset.from_dict(data)
        result = evaluate(ds, metrics=metrics)

        return RagasResult(
            faithfulness=result.get("faithfulness"),
            context_recall=result.get("context_recall"),
        )
    except Exception as e:
        logger.warning(f"Ragas 评测失败（降级为 None）: {e}")
        return None


def extract_contexts_from_working_memory(
    working_memory: list[dict[str, Any]],
) -> list[str]:
    """从 working_memory 中提取 search_sop/kb_search 的文本内容作为 contexts。"""
    contexts: list[str] = []
    for rec in working_memory:
        tool_name = rec.get("tool_name", "")
        if tool_name not in ("search_sop", "kb_search"):
            continue
        if not rec.get("success"):
            continue
        result = rec.get("result")
        if not result:
            continue
        # 提取文档内容
        data = result if isinstance(result, list) else result.get("data", [])
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    # 取 content / text / snippet 等文本字段
                    for field in ("content", "text", "snippet", "title"):
                        if item.get(field):
                            contexts.append(str(item[field]))
                            break
                elif isinstance(item, str):
                    contexts.append(item)
    return contexts
