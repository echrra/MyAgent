"""真实工具集 —— W1 接入 W0 产出的合成日志与知识库。

设计原则：
- 对外返回结构与 mock.py 完全一致（{"data": ..., "meta": ...}），便于无痛替换
- 数据路径走 config，不硬编码
- 文件解析结果用 lru_cache 缓存（数据量小，全量进内存）
- W1 仅做关键词检索；向量 RAG 在 W2 接入
"""

import glob
import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

from loguru import logger

from opsagent.core.config import settings
from opsagent.core.tools.tls_client import (
    is_enabled as _tls_enabled,
    query_by_service_keyword as _tls_query_service,
    query_by_trace as _tls_query_trace,
)

# ====================== search_logs ======================

# glog 等级严重度排序（用于命中结果排序：错误优先展示）
_LEVEL_SEVERITY = {"FATA": 4, "ERRO": 3, "WARN": 2, "INFO": 1, "DEBU": 0}

# 合成日志只取 Claude 仿写版（排除旧的 _seedNN 实验语料，避免重复）
_LOG_GLOB = "fault_F*_claude.jsonl"


@lru_cache(maxsize=1)
def _load_all_logs() -> tuple[dict[str, Any], ...]:
    """加载并归一化全部合成日志行（进程级缓存）。

    把 GoFrame glog 字段映射成与 mock 一致的对外结构：
        time → ts, Level → level, TraceId → trace_id
    其余 service/msg/status_code/method/latency_ms 原样保留，
    Content/CallerPath 作为可选 detail 字段。
    """
    rows: list[dict[str, Any]] = []
    pattern = os.path.join(settings.logs_dir, _LOG_GLOB)
    files = sorted(glob.glob(pattern))
    if not files:
        logger.warning(f"[search_logs] 未找到日志文件: {pattern}")

    for fp in files:
        with open(fp, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    raw = json.loads(line)
                except json.JSONDecodeError:
                    # 跳过个别坏行，保证健壮性
                    continue
                norm = {
                    "ts": raw.get("time", ""),
                    "level": raw.get("Level", ""),
                    "trace_id": raw.get("TraceId", ""),
                    "service": raw.get("service", ""),
                    "msg": raw.get("msg", ""),
                    "status_code": raw.get("status_code"),
                    "method": raw.get("method"),
                    "latency_ms": raw.get("latency_ms"),
                }
                # ERRO 多行体作为可选 detail（便于关键词匹配 Stack/Error 内容）
                detail = raw.get("Content") or raw.get("CallerPath")
                if detail:
                    norm["detail"] = detail
                rows.append(norm)

    logger.info(f"[search_logs] 已加载 {len(rows)} 行合成日志（{len(files)} 个文件）")
    return tuple(rows)


def search_logs(service: str, keyword: str = "", minutes: int = 10, at: str = "") -> dict[str, Any]:
    """按服务名 + 关键词查合成日志（本地）；本地无命中且 TLS fallback 开启时，自动查云端。

    Args:
        service: 服务名（如 edgectl-backend-http / edgectl-admin / edgectl-backend-watcher）
        keyword: 关键词，匹配 msg + detail 子串（大小写不敏感）；为空则只按 service 过滤
        minutes: 时间窗（分钟）。本地评测集时间固定不做过滤；云端 fallback 时启用。
        at: 锚点时间（ISO8601，如 "2026-06-17T13:49:53Z"）。给了则以 at 为中心
            前后 minutes/2 查云端；不给则从"现在"往前推 minutes 分钟。
            本地检索目前忽略 at（评测集时间固定），fallback 时启用。

    Returns:
        {"data": [日志行...], "meta": {...}}，最多返回 20 条（错误优先）。
        meta.source 标注数据来源："local" / "tls_fallback" / "hybrid"。
    """
    rows = _load_all_logs()
    svc = service.strip().lower()
    kw = keyword.strip().lower()

    hits = []
    for r in rows:
        if r["service"].lower() != svc:
            continue
        if kw:
            haystack = (r["msg"] + " " + str(r.get("detail", ""))).lower()
            if kw not in haystack:
                continue
        hits.append(r)

    # 本地命中不足时，若开启 fallback 就补一批云端数据
    source = "local"
    if _tls_enabled() and len(hits) == 0:
        cloud_rows = _tls_query_service(agent_service=service, keyword=keyword, at=at, minutes=minutes)
        if cloud_rows:
            hits.extend(cloud_rows)
            source = "tls_fallback"

    # 错误优先排序，最多 20 条防 prompt 爆炸
    hits.sort(key=lambda r: _LEVEL_SEVERITY.get(r["level"], 0), reverse=True)
    returned = hits[:20]

    return {
        "data": returned,
        "meta": {
            "service": service,
            "keyword": keyword,
            "minutes": minutes,
            "at": at,
            "source": source,
            "hit": len(hits),
            "returned": len(returned),
        },
    }


# ====================== search_sop ======================


@lru_cache(maxsize=1)
def _load_all_docs() -> tuple[dict[str, Any], ...]:
    """加载知识库全部 md（进程级缓存）。

    每篇解析为 doc_id（文件名去 .md）/ title（首个 # 行）/ category（父目录名）/
    text（全文）+ 小写副本（供大小写不敏感匹配）。跳过 README.md。
    """
    docs: list[dict[str, Any]] = []
    base = Path(settings.docs_dir)
    if not base.exists():
        logger.warning(f"[search_sop] 知识库目录不存在: {base}")
        return tuple()

    for md in sorted(base.rglob("*.md")):
        if md.name.lower() == "readme.md":
            continue
        text = md.read_text(encoding="utf-8")
        # 标题取首个 Markdown # 行，缺失则用文件名
        title = md.stem
        for ln in text.splitlines():
            if ln.lstrip().startswith("# "):
                title = ln.lstrip()[2:].strip()
                break
        docs.append({
            "doc_id": md.stem,
            "title": title,
            "category": md.parent.name,
            "text": text,
            "title_lower": title.lower(),
            "text_lower": text.lower(),
        })

    logger.info(f"[search_sop] 已加载 {len(docs)} 篇知识库文档")
    return tuple(docs)


def _make_snippet(text: str, first_hit_pos: int) -> str:
    """从命中位置前后各取约 80 字作摘要；无命中则取正文前 120 字。"""
    if first_hit_pos < 0:
        snippet = text[:120].strip()
    else:
        start = max(0, first_hit_pos - 80)
        snippet = text[start:first_hit_pos + 80].strip()
    # 去掉多余空行，单行展示更整洁
    return " ".join(snippet.split())


def _chunk_snippet(content: str, limit: int = 160) -> str:
    """从 chunk 正文取摘要：去掉 [title] section 前缀，单行展示。"""
    body = content.split("\n\n", 1)[-1] if "\n\n" in content else content
    return " ".join(body.split())[:limit]


def search_sop(query: str, top_k: int = 5) -> dict[str, Any]:
    """混合检索知识库（SOP / 复盘 / runbook / 服务说明）。

    W2：向量召回 + BM25 → RRF 融合 → bge-reranker 精排（见 core/retrieval）。
    返回结构与 W1 完全一致，下游 plan/answer 节点无感切换。
    DB 未起 / 索引未建 / 模型不可用时，自动降级为 W1 关键词检索，保证 demo 不硬崩。

    Args:
        query: 检索词
        top_k: 返回条数上限

    Returns:
        {"data": [{doc_id, title, snippet, score, category, section, source_lines}...],
         "meta": {...}}
    """
    # 延迟 import：避免没装 torch/未起库的环境 import 本模块即失败
    try:
        from opsagent.core.retrieval.pipeline import retrieve

        out = retrieve(query, top_k=top_k)
        data = [
            {
                "doc_id": r["doc_id"],
                "title": r["title"],
                "snippet": _chunk_snippet(r["content"]),
                "score": r["score"],
                "category": r["category"],
                "section": r["section"],
                "source_lines": r["source_lines"],
            }
            for r in out["results"]
        ]
        return {
            "data": data,
            "meta": {
                "query": query,
                "top_k": top_k,
                "mode": "hybrid",
                "recall_vec": out["recall_vec"],
                "recall_bm25": out["recall_bm25"],
                "reranked": out["reranked"],
            },
        }
    except Exception as exc:  # 检索链路任意环节失败都降级，不让工具崩
        logger.warning(f"[search_sop] 混合检索不可用，降级关键词检索: {exc}")
        return _search_sop_keyword(query, top_k)


def _search_sop_keyword(query: str, top_k: int = 3) -> dict[str, Any]:
    """关键词检索降级路径（W1 原逻辑）：标题/正文子串命中打分。"""
    docs = _load_all_docs()
    # 拆 token：去空白 + 去重 + 过滤空串
    tokens = [t for t in {w.strip().lower() for w in query.split()} if t]
    if not tokens:
        return {"data": [], "meta": {"query": query, "n_hit": 0, "top_k": top_k}}

    scored = []
    for d in docs:
        title_hits = sum(1 for t in tokens if t in d["title_lower"])
        text_hits = sum(1 for t in tokens if t in d["text_lower"])
        cat_hits = sum(1 for t in tokens if t in d["category"].lower())
        # 标题命中权重最高，正文次之，分类辅助
        raw = 3.0 * title_hits + 1.0 * text_hits + 0.5 * cat_hits
        if raw <= 0:
            continue
        # 软上限归一化到 0-1
        score = round(raw / (raw + 3.0), 3)

        # 取首个命中 token 在正文的位置做 snippet
        first_pos = -1
        for t in tokens:
            pos = d["text_lower"].find(t)
            if pos >= 0:
                first_pos = pos
                break
        scored.append({
            "doc_id": d["doc_id"],
            "title": d["title"],
            "snippet": _make_snippet(d["text"], first_pos),
            "score": score,
            "category": d["category"],
        })

    scored.sort(key=lambda x: x["score"], reverse=True)
    top = scored[:top_k]

    return {
        "data": top,
        "meta": {"query": query, "n_hit": len(scored), "top_k": top_k, "mode": "keyword"},
    }


# ====================== trace_query ======================

# 视为「错误」的日志等级（GoFrame glog 4 字符写法）：用于判断一条链路是否出过错。
_ERROR_LEVELS = {"FATA", "ERRO"}
# 模式 B（按服务列可疑链路）默认最多返回多少条 trace，避免一次塞太多。
# base.Tool 还会按 settings.tool_max_list_items 再兜一道截断。
_TRACE_LIST_LIMIT = 10


def _trace_max_latency(lines: list[dict[str, Any]]) -> tuple[int, str]:
    """从一组日志行里取最大 latency_ms 及其所属服务。无 latency 则返回 (0, "")。"""
    best_ms = 0
    best_svc = ""
    for ln in lines:
        lat = ln.get("latency_ms")
        if isinstance(lat, (int, float)) and lat > best_ms:
            best_ms = int(lat)
            best_svc = ln.get("service", "")
    return best_ms, best_svc


def _uniq_keep_order(items: list[str]) -> list[str]:
    """按出现顺序去重（保留首次出现次序），用于「链路经过的服务列表」。"""
    seen: set[str] = set()
    out: list[str] = []
    for it in items:
        if it and it not in seen:
            seen.add(it)
            out.append(it)
    return out


def _timeline_item(r: dict[str, Any]) -> dict[str, Any]:
    """构造 trace timeline 单行；错误行额外带原始日志片段。"""
    item = {
        "ts": r.get("ts", ""),
        "service": r.get("service", ""),
        "level": r.get("level", ""),
        "msg": r.get("msg", ""),
        "latency_ms": r.get("latency_ms"),
        "status_code": r.get("status_code"),
    }
    detail = str(r.get("detail", ""))
    if r.get("level") in _ERROR_LEVELS | {"WARN"} and detail:
        item["detail_snippet"] = detail[:2000]
    return item


def _error_log_item(r: dict[str, Any]) -> dict[str, Any]:
    """构造原始错误日志条目，只做截断，不提炼原因。"""
    return {
        "ts": r.get("ts", ""),
        "service": r.get("service", ""),
        "level": r.get("level", ""),
        "msg": r.get("msg", ""),
        "detail_snippet": str(r.get("detail", ""))[:3000],
    }


def trace_query(
    trace_id: str = "", service: str = "", minutes: int = 30, at: str = ""
) -> dict[str, Any]:
    """查调用链路。本地无命中 + TLS fallback 开启时，trace_id 模式会自动查云端。

    数据说明（诚实标注）：本项目尚未合成独立 trace/span 数据，这里是**从合成日志按
    TraceId 聚合重建的「调用时间线」**——不是真实 APM 的 span 父子树，没有 span 间
    parent/child 关系，只有「同一 trace_id 的跨服务日志行按时间排序」。meta.derived_from
    标注来源，便于上层区分数据来源。

    两种模式（至少给 trace_id / service 其一，入参校验在注册层强制）：
    - 给 trace_id（模式 A）：返回该链路的跨服务时间线 + 汇总（经过的服务 / 跨度 / 错误数 /
      最慢服务），用于「下钻看一条具体链路发生了什么」。**本地未命中且 fallback 开启时
      会自动查 TLS 云端**（真实生产链路查询正是靠这条通路）。
    - 只给 service（模式 B）：返回该服务近期可疑链路列表（按是否出错、最大延迟降序），
      让 Agent 挑一个 trace_id 再下钻，或转 search_logs 看细节，形成工具链。

    Args:
        trace_id: 链路 ID；给定则走模式 A。
        service: 服务名；仅给 service 时走模式 B。
        minutes: 时间窗（分钟）。合成日志时间固定，暂只记入 meta 不实际过滤；
                 云端 fallback 时启用。
        at: 锚点时间（ISO8601）。给了则以 at 为中心前后 minutes/2 查云端；
            不给则从"现在"往前推。用于查历史故障（例如用户报 2026-06-17 13:49 的 trace）。

    Returns:
        {"data": {...}, "meta": {...}}。meta.source 标注："local" / "tls_fallback"。
    """
    rows = _load_all_logs()

    # ---------- 模式 A：按 trace_id 还原单条链路时间线 ----------
    if trace_id.strip():
        tid = trace_id.strip()
        lines = [r for r in rows if r.get("trace_id") == tid]
        source = "local"

        # 本地未命中且 fallback 开启：查云端 TLS
        if not lines and _tls_enabled():
            cloud_lines = _tls_query_trace(trace_id=tid, at=at, minutes=minutes)
            if cloud_lines:
                lines = cloud_lines
                source = "tls_fallback"

        # 按时间戳升序（ISO8601 同偏移量下字典序即时间序）
        lines.sort(key=lambda r: r.get("ts", ""))
        timeline = [_timeline_item(r) for r in lines]
        max_ms, slow_svc = _trace_max_latency(lines)
        services = _uniq_keep_order([r.get("service", "") for r in lines])
        error_count = sum(1 for r in lines if r.get("level") in _ERROR_LEVELS)
        issue_logs = [_error_log_item(r) for r in lines if r.get("level") in (_ERROR_LEVELS | {"WARN"})]
        summary = {
            "services": services,
            "n_spans": len(lines),
            "error_count": error_count,
            "max_latency_ms": max_ms,
            "slowest_service": slow_svc,
            "start_ts": lines[0]["ts"] if lines else "",
            "end_ts": lines[-1]["ts"] if lines else "",
        }
        return {
            "data": {
                "trace_id": tid,
                "issue_logs": issue_logs,
                "timeline": timeline,
                "summary": summary,
            },
            "meta": {
                "mode": "by_trace",
                "derived_from": "logs_by_trace_id",
                "trace_id": tid,
                "minutes": minutes,
                "at": at,
                "source": source,
                "found": len(lines),
            },
        }

    # ---------- 模式 B：按服务列近期可疑链路 ----------
    svc = service.strip().lower()
    # 先收集「经过该服务」的所有 trace_id
    target_tids = {
        r["trace_id"]
        for r in rows
        if r.get("trace_id") and r.get("service", "").lower() == svc
    }
    # 再按 trace_id 把整条链路（跨服务）的行聚到一起
    groups: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        tid = r.get("trace_id")
        if tid in target_tids:
            groups.setdefault(tid, []).append(r)

    traces = []
    for tid, lines in groups.items():
        max_ms, _ = _trace_max_latency(lines)
        traces.append({
            "trace_id": tid,
            "n_spans": len(lines),
            "has_error": any(r.get("level") in _ERROR_LEVELS for r in lines),
            "max_latency_ms": max_ms,
            "services": _uniq_keep_order([r.get("service", "") for r in lines]),
        })
    # 可疑优先：先出过错的，再延迟大的
    traces.sort(key=lambda t: (t["has_error"], t["max_latency_ms"]), reverse=True)
    returned = traces[:_TRACE_LIST_LIMIT]

    return {
        "data": {"service": service, "traces": returned},
        "meta": {
            "mode": "by_service",
            "derived_from": "logs_by_trace_id",
            "service": service,
            "minutes": minutes,
            "hit": len(traces),
            "returned": len(returned),
        },
    }
