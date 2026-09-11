"""工具轨迹落盘 —— 把每次工具调用的完整细节结构化写入 JSONL，供 badcase 归因。

为什么要这个模块：
E040 为什么稳定 0.100？只能靠 docs/11 §八 那种手动复现检索。根因是工具调用的
实际 query、返回的 doc_id、rerank 分数、耗时、错误全部没落盘。本模块把这些
按 case 维度组织成 JSONL 轨迹文件，归因从「人肉翻文档」变成「查 JSONL」。

数据流：
  runner._invoke_and_score() → collect_case_trace() → save_trace() → JSONL 落盘

输出格式（每条 case 一个 JSONL 文件，每次工具调用一行）：
  {"seq": 0, "tool_name": "search_sop", "args": {"query": "..."}, "success": true,
   "latency_ms": 1234, "error": null, "doc_ids": [...], "doc_scores": [...],
   "truncated": false, "result_chars": 543}

用法：
  from eval.trace import collect_case_trace, save_trace
  trace = collect_case_trace(case_id, working_memory, latency_ms, error)
  save_trace(trace, Path("eval/traces"))
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ====================== 数据结构 ======================


@dataclass
class ToolCallTrace:
    """单次工具调用的轨迹记录。"""

    seq: int                     # 调用序号（该 case 内第几次调用）
    tool_name: str               # 工具名
    args: dict[str, Any]         # 调用参数（完整保留）
    success: bool                # 是否成功
    latency_ms: int              # 耗时（毫秒）
    error: str | None = None     # 错误信息
    # 检索类工具的结构化提取（方便归因，不用翻原始 result）
    doc_ids: list[str] = field(default_factory=list)      # 返回的文档 ID
    doc_scores: list[float] = field(default_factory=list)  # 对应的 rerank/检索分数
    truncated: bool = False      # 输出是否被截断
    result_chars: int = 0        # 结果序列化后的字符数（衡量信息量）


@dataclass
class CaseTrace:
    """一条 case 的完整运行轨迹。"""

    case_id: str
    timestamp: str               # ISO8601
    total_latency_ms: float      # case 级总耗时
    n_tool_calls: int            # 工具调用总数
    error: str | None = None     # case 级错误（超时等）
    wiki_jumps: list[str] = field(default_factory=list)  # wiki_read 跳转路径
    tool_calls: list[ToolCallTrace] = field(default_factory=list)


# ====================== 内部工具 ======================


def _extract_doc_info(result: Any) -> tuple[list[str], list[float]]:
    """从工具返回的 result 中提取 doc_id 和分数。

    覆盖 search_sop / kb_search / wiki_read 三种检索工具的返回格式。
    """
    doc_ids: list[str] = []
    doc_scores: list[float] = []

    if not isinstance(result, dict):
        return doc_ids, doc_scores

    data = result.get("data")
    if not isinstance(data, list):
        return doc_ids, doc_scores

    for item in data:
        if not isinstance(item, dict):
            continue
        # doc_id：优先取显式字段
        for key in ("doc_id", "id", "path", "title"):
            if key in item:
                doc_ids.append(str(item[key]))
                break
        # 分数：rerank_score > score > rrf_score
        for key in ("rerank_score", "score", "rrf_score"):
            if key in item and isinstance(item[key], (int, float)):
                doc_scores.append(float(item[key]))
                break

    return doc_ids, doc_scores


def _extract_wiki_jumps(tool_calls: list[ToolCallTrace]) -> list[str]:
    """从 wiki_read 调用序列提取跳转路径。

    wiki_read 的 doc_ids 参数代表「读了哪些文档」，连续的 wiki_read 序列
    构成 Agent 在知识图谱中的跳转路径。
    """
    jumps: list[str] = []
    for call in tool_calls:
        if call.tool_name != "wiki_read":
            continue
        doc_ids_str = call.args.get("doc_ids", "")
        if doc_ids_str:
            for doc_id in doc_ids_str.split(","):
                doc_id = doc_id.strip()
                if doc_id:
                    jumps.append(doc_id)
    return jumps


def _estimate_chars(obj: Any) -> int:
    """估算对象序列化后的字符数。"""
    try:
        return len(json.dumps(obj, ensure_ascii=False, default=str))
    except (TypeError, ValueError):
        return len(str(obj))


# ====================== 公开接口 ======================


def collect_case_trace(
    case_id: str,
    working_memory: list[dict[str, Any]],
    latency_ms: float,
    error: str | None = None,
) -> CaseTrace:
    """从 working_memory 提取一条 case 的完整轨迹。

    Args:
        case_id: 评测 case ID
        working_memory: graph 返回的 working_memory（ToolCallRecord 列表）
        latency_ms: case 级总耗时
        error: case 级错误（如超时）

    Returns:
        结构化的 CaseTrace
    """
    tool_traces: list[ToolCallTrace] = []

    for seq, record in enumerate(working_memory):
        result = record.get("result")
        doc_ids, doc_scores = _extract_doc_info(result)

        # 检测截断标记（Tool 层写入 meta.truncated）
        truncated = False
        if isinstance(result, dict):
            meta = result.get("meta") or {}
            truncated = bool(meta.get("truncated", False))

        tool_traces.append(ToolCallTrace(
            seq=seq,
            tool_name=record.get("tool_name", ""),
            args=record.get("args") or {},
            success=record.get("success", False),
            latency_ms=record.get("latency_ms", 0),
            error=record.get("error"),
            doc_ids=doc_ids,
            doc_scores=doc_scores,
            truncated=truncated,
            result_chars=_estimate_chars(result),
        ))

    wiki_jumps = _extract_wiki_jumps(tool_traces)

    return CaseTrace(
        case_id=case_id,
        timestamp=datetime.now(timezone.utc).isoformat(),
        total_latency_ms=latency_ms,
        n_tool_calls=len(tool_traces),
        error=error,
        wiki_jumps=wiki_jumps,
        tool_calls=tool_traces,
    )


def save_trace(trace: CaseTrace, output_dir: Path) -> Path:
    """轨迹落盘为 JSONL（每次工具调用一行，便于 grep / jq）。

    输出到 output_dir/{case_id}.jsonl。

    Returns:
        写入的文件路径
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{trace.case_id}.jsonl"

    lines: list[str] = []
    # 第一行：case 级元信息
    header = {
        "_type": "case_header",
        "case_id": trace.case_id,
        "timestamp": trace.timestamp,
        "total_latency_ms": trace.total_latency_ms,
        "n_tool_calls": trace.n_tool_calls,
        "error": trace.error,
        "wiki_jumps": trace.wiki_jumps,
    }
    lines.append(json.dumps(header, ensure_ascii=False, default=str))

    # 后续每行：一次工具调用
    for call in trace.tool_calls:
        record = asdict(call)
        record["_type"] = "tool_call"
        lines.append(json.dumps(record, ensure_ascii=False, default=str))

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def cleanup_old_traces(trace_root: Path, keep_last_n: int = 10) -> int:
    """保留最近 N 轮 trace 目录，清理更早的。

    调用方决定何时清理（runner 不自动调，避免误删用户想保留的 trace）。
    建议 CLI 入口里跑：`python -c "from eval.trace import cleanup_old_traces; cleanup_old_traces(Path('eval/traces'))"`

    Args:
        trace_root: eval/traces 根目录
        keep_last_n: 保留最近多少轮（按目录名排序，run_id 自带时间戳，名字排序=时间排序）

    Returns:
        删除的目录数
    """
    if not trace_root.is_dir():
        return 0
    # run_id 形如 eval_20260907_174930_80c244，按字典序排=按时间排
    run_dirs = sorted([p for p in trace_root.iterdir() if p.is_dir()])
    to_remove = run_dirs[:-keep_last_n] if len(run_dirs) > keep_last_n else []
    import shutil
    for d in to_remove:
        shutil.rmtree(d, ignore_errors=True)
    return len(to_remove)


def load_traces(trace_dir: Path, case_id: str | None = None) -> list[CaseTrace]:
    """从目录加载轨迹文件。

    Args:
        trace_dir: 轨迹目录
        case_id: 可选，只加载指定 case 的轨迹

    Returns:
        CaseTrace 列表
    """
    if not trace_dir.is_dir():
        return []

    pattern = f"{case_id}.jsonl" if case_id else "*.jsonl"
    traces: list[CaseTrace] = []

    for f in sorted(trace_dir.glob(pattern)):
        header: dict[str, Any] = {}
        tool_calls: list[ToolCallTrace] = []

        for line in f.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue

            if record.get("_type") == "case_header":
                header = record
            elif record.get("_type") == "tool_call":
                record.pop("_type", None)
                tool_calls.append(ToolCallTrace(**{
                    k: v for k, v in record.items()
                    if k in ToolCallTrace.__dataclass_fields__
                }))

        traces.append(CaseTrace(
            case_id=header.get("case_id", f.stem),
            timestamp=header.get("timestamp", ""),
            total_latency_ms=header.get("total_latency_ms", 0.0),
            n_tool_calls=header.get("n_tool_calls", 0),
            error=header.get("error"),
            wiki_jumps=header.get("wiki_jumps", []),
            tool_calls=tool_calls,
        ))

    return traces
