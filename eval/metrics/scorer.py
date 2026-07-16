"""L4 端到端 scorer —— 根据评测 case 的期望值给 Agent 输出打分。

公式（按难度分权）：
  easy:   total = max(0, 0.40*tool + 0.30*cite + 0.30*conc - forbidden)
  medium: total = max(0, 0.20*tool + 0.40*cite + 0.40*conc - forbidden)
  hard:   total = max(0, 0.10*tool + 0.40*cite + 0.50*conc - forbidden)

设计：
- tool_recall：Recall-only，支持 tools 列表（命中任一即可）+ 单 tool 精确匹配
- citation：二值评分（精确命中 1.0 / 未命中 0.0），matched/expected
- forbidden_pen：0.1/词累加，封顶 0.3
- 难度分权：easy 侧重工具精确性，medium/hard 侧重输出质量
- 所有函数纯计算（无 IO / 无 LLM），便于单测
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


@dataclass
class EvalResult:
    """单条评测结果。"""

    case_id: str
    total_score: float
    tool_score: float
    citation_score: float
    conclusion_score: float
    forbidden_penalty: float
    # 明细——用于 badcase 分析
    matched_tools: list[str] = field(default_factory=list)
    missing_tools: list[str] = field(default_factory=list)
    extra_tools: list[str] = field(default_factory=list)
    matched_citations: list[str] = field(default_factory=list)
    matched_keywords: list[str] = field(default_factory=list)
    triggered_forbidden: list[str] = field(default_factory=list)


# ====================== 子评分函数 ======================


def tool_sequence_score(
    actual_calls: list[dict[str, Any]],
    expected_sequence: list[dict[str, Any]],
) -> tuple[float, list[str], list[str], list[str]]:
    """工具调用 Recall 评分 —— 覆盖期望工具即满分，多调不扣分。

    支持两种 expected 格式：
    - {"tool": "search_logs", "args_match": {...}}  精确匹配单工具
    - {"tools": ["search_logs", "trace_query", ...], "args_match": {...}}  命中任一即可

    返回: (score, matched_names, missing_names, extra_names)
    """
    if not expected_sequence:
        return 1.0, [], [], [c.get("tool_name", "") for c in actual_calls]
    if not actual_calls:
        missing = [_exp_label(e) for e in expected_sequence]
        return 0.0, [], missing, []

    # 贪心匹配：每个 expected 找第一个未使用的 actual
    used_indices: set[int] = set()
    matched: list[str] = []
    missing: list[str] = []

    for exp in expected_sequence:
        exp_tools = _exp_tool_set(exp)
        exp_args = exp.get("args_match", {}) or {}
        found = False
        for idx, act in enumerate(actual_calls):
            if idx in used_indices:
                continue
            act_tool = act.get("tool_name", "")
            if act_tool not in exp_tools:
                continue
            # 检查 args 子串匹配
            if _args_match(act.get("args", {}), exp_args):
                used_indices.add(idx)
                matched.append(act_tool)
                found = True
                break
        if not found:
            missing.append(_exp_label(exp))

    # extra = actual 中未被匹配到的（仅记录，不影响分数）
    extra = [
        actual_calls[i].get("tool_name", "")
        for i in range(len(actual_calls))
        if i not in used_indices
    ]

    # Recall-only：覆盖所有期望工具即满分，多调不扣分
    recall = len(matched) / len(expected_sequence)

    return recall, matched, missing, extra


def citation_score(
    final_answer: str,
    working_memory: list[dict[str, Any]],
    expected_citations: list[str],
) -> tuple[float, list[str]]:
    """引用命中 —— 二值评分（精确命中 1.0 / 未命中 0.0）。

    返回: (score, matched_list)
    """
    if not expected_citations:
        return 1.0, []

    # 收集 Agent 实际引用的文档标识
    actual_refs = _extract_citations(final_answer, working_memory)

    matched: list[str] = []

    for exp in expected_citations:
        exp_lower = exp.lower()
        # 精确匹配：子串命中
        exact_hit = False
        for ref in actual_refs:
            if exp_lower in ref.lower():
                exact_hit = True
                break
        if not exact_hit and exp_lower in final_answer.lower():
            exact_hit = True

        if exact_hit:
            matched.append(exp)

    score = len(matched) / len(expected_citations)
    return score, matched



def conclusion_score(
    final_answer: str,
    expected_keywords: list[str],
    expected_keywords_min: int,
) -> tuple[float, list[str]]:
    """结论关键词命中 —— min(命中数/期望最少数, 1.0)。

    返回: (score, matched_keywords)
    """
    if not expected_keywords or expected_keywords_min <= 0:
        return 1.0, []
    if not final_answer:
        return 0.0, []

    answer_lower = final_answer.lower()
    matched = [kw for kw in expected_keywords if str(kw).lower() in answer_lower]
    score = min(len(matched) / expected_keywords_min, 1.0)
    return score, matched


def forbidden_penalty(
    final_answer: str,
    forbidden_keywords: list[str],
) -> tuple[float, list[str]]:
    """forbidden 扣分 —— 每命中一个词 -0.1，封顶 -0.3。

    返回: (penalty_value, triggered_list)  penalty_value >= 0
    """
    if not forbidden_keywords or not final_answer:
        return 0.0, []

    answer_lower = final_answer.lower()
    triggered = [kw for kw in forbidden_keywords if str(kw).lower() in answer_lower]
    # 累加制：0.1/词，封顶 0.3
    penalty = min(len(triggered) * 0.1, 0.3)
    return penalty, triggered


# ====================== 难度权重 ======================

# easy 侧重工具精确性，medium/hard 侧重输出质量
DIFFICULTY_WEIGHTS: dict[str, dict[str, float]] = {
    "easy": {"tool": 0.40, "cite": 0.30, "conc": 0.30},
    "medium": {"tool": 0.20, "cite": 0.40, "conc": 0.40},
    "hard": {"tool": 0.10, "cite": 0.40, "conc": 0.50},
}


# ====================== 主入口 ======================


def score_case(
    case: dict[str, Any],
    actual_calls: list[dict[str, Any]],
    final_answer: str,
) -> EvalResult:
    """按难度分权公式算总分，返回完整 EvalResult。

    Args:
        case: 评测用例 dict（从 YAML 加载，需含 difficulty 字段）
        actual_calls: Agent 的 working_memory（ToolCallRecord 列表）
        final_answer: Agent 最终回答文本
    """
    # 各维度打分
    t_score, t_matched, t_missing, t_extra = tool_sequence_score(
        actual_calls, case.get("expected_tool_sequence", [])
    )
    c_score, c_matched = citation_score(
        final_answer,
        actual_calls,
        case.get("expected_citations", []),
    )
    k_score, k_matched = conclusion_score(
        final_answer,
        case.get("expected_keywords", []),
        case.get("expected_keywords_min", 0),
    )
    f_penalty, f_triggered = forbidden_penalty(
        final_answer, case.get("forbidden_keywords", [])
    )

    # 按难度取权重，默认 easy
    difficulty = case.get("difficulty", "easy")
    w = DIFFICULTY_WEIGHTS.get(difficulty, DIFFICULTY_WEIGHTS["easy"])
    total = max(0.0, w["tool"] * t_score + w["cite"] * c_score + w["conc"] * k_score - f_penalty)

    return EvalResult(
        case_id=case.get("id", "?"),
        total_score=round(total, 4),
        tool_score=round(t_score, 4),
        citation_score=round(c_score, 4),
        conclusion_score=round(k_score, 4),
        forbidden_penalty=round(f_penalty, 4),
        matched_tools=t_matched,
        missing_tools=t_missing,
        extra_tools=t_extra,
        matched_citations=c_matched,
        matched_keywords=k_matched,
        triggered_forbidden=f_triggered,
    )


# ====================== 内部工具 ======================


def _exp_tool_set(exp: dict[str, Any]) -> set[str]:
    """从 expected item 提取可接受的工具名集合。"""
    if "tools" in exp:
        return set(exp["tools"])
    return {exp.get("tool", "")}


def _exp_label(exp: dict[str, Any]) -> str:
    """expected item 的展示标签（用于 missing 列表）。"""
    if "tools" in exp:
        return "|".join(exp["tools"])
    return exp.get("tool", "")


def _args_match(actual_args: dict[str, Any], expected_args: dict[str, Any]) -> bool:
    """宽松参数匹配：expected 中每个 key 的值在 actual 中做子串比对。"""
    for key, exp_val in expected_args.items():
        act_val = actual_args.get(key)
        if act_val is None:
            return False
        # 转字符串做子串匹配（不区分大小写）
        if str(exp_val).lower() not in str(act_val).lower():
            return False
    return True


def _extract_citations(
    final_answer: str, working_memory: list[dict[str, Any]]
) -> list[str]:
    """从工具结果和回答文本中提取文档引用标识。"""
    refs: list[str] = []

    # 从 search_sop / kb_search 的结果中提取 doc_id / title / path
    for rec in working_memory:
        tool_name = rec.get("tool_name", "")
        if tool_name not in ("search_sop", "kb_search"):
            continue
        result = rec.get("result")
        if not result:
            continue
        # result 结构: {"data": [...], "meta": {...}}
        data = result if isinstance(result, list) else result.get("data", [])
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    # 取 doc_id / title / path 任一
                    for field in ("doc_id", "title", "path", "id"):
                        if field in item:
                            refs.append(str(item[field]))

    # 从 final_answer 中提取常见引用模式（如 SOP-F1、PM-001 等）
    patterns = [
        r"sop[-_]f\d+[-\w]*",
        r"pm[-_]\d+[-\w]*",
        r"runbook[-_]\w+",
    ]
    for pat in patterns:
        refs.extend(re.findall(pat, final_answer, re.IGNORECASE))

    return refs
