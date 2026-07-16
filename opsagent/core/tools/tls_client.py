"""TLS 云端日志 fallback 客户端 —— 只在 agent 显式开启时才打云端。

设计要点：
1. 默认关闭：需环境变量 AGENT_ENABLE_TLS_FALLBACK=1 才开；关闭时 is_enabled() 返回 False，
   工具静默走本地。避免每次跑合成 case 都打云端（贵/慢/rate-limit）。
2. 反向 service 映射：agent 用别名 edgectl-*（合成评测集用的），真实 TLS 用 linkcraft-*。
   fallback 时把 agent 传入的 service 反向翻译回去，再补真实名做查询。
3. PII 脱敏强制走：云端结果落回 agent 前必须过 sanitizer，避免 R3 那个 LLM 静默拒答问题复发。
4. 静默降级：TLS_TOPIC_ID / TOS_ACCESS_KEY 缺失时 is_enabled() 返回 False，不抛异常，
   工具端仍返回本地结果 —— 让"没配就用不了 fallback"这件事变成软失败而非硬崩。
5. 双池注意：fallback 走网络 I/O（TLS SDK 是同步阻塞），由上层 Tool wrapper 决定跑在哪个池。
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any

from loguru import logger

from opsagent.core.utils.pii_sanitizer import sanitize as _sanitize

# 反向映射：agent 用的别名 → 真实生产服务名（TLS __container_name__）
# 与 scripts/tls_to_search_logs.py 的正向表一一对应
_SERVICE_ALIAS_TO_REAL: dict[str, list[str]] = {
    "edgectl-backend-http": ["linkcraft-backend-http", "linkcraft-ugc"],
    "edgectl-backend-watcher": ["linkcraft-backend-watcher"],
    "edgectl-backend-scheduler": ["linkcraft-backend-scheduler"],
    "edgectl-admin": ["linkcraft-admin"],
}

# 云端查询默认 topic（prod-linkcraft），可被环境变量覆盖
_DEFAULT_TOPIC_ENV = "TLS_PROD_TOPIC_ID"

# 单次云端查询返回上限（TLS SearchLogsV2 limit）
_TLS_LIMIT = 100


def is_enabled() -> bool:
    """判断是否启用 TLS fallback：开关开 + 凭证齐 + topic 配置齐。"""
    if os.getenv("AGENT_ENABLE_TLS_FALLBACK", "0") not in {"1", "true", "TRUE", "yes"}:
        return False
    if not os.getenv("TOS_ACCESS_KEY") or not os.getenv("TOS_SECRET_KEY"):
        return False
    if not os.getenv(_DEFAULT_TOPIC_ENV):
        return False
    return True


def _get_topic_id() -> str:
    return os.getenv(_DEFAULT_TOPIC_ENV, "").strip()


def _get_client():
    """懒加载 TLS 客户端；调用方先用 is_enabled() 判空。"""
    from volcengine.tls.TLSService import TLSService  # type: ignore

    ak = os.getenv("TOS_ACCESS_KEY", "")
    sk = os.getenv("TOS_SECRET_KEY", "")
    endpoint = os.getenv("TLS_ENDPOINT", "https://tls-cn-shanghai.volces.com")
    region = os.getenv("TLS_REGION", "cn-shanghai")
    return TLSService(endpoint=endpoint, access_key_id=ak, access_key_secret=sk, region=region)


def _parse_at(at: str | None) -> datetime | None:
    """解析用户给的锚点时间 at；容错 ISO8601 / 空格分隔的常见写法。"""
    if not at:
        return None
    s = at.strip().replace("Z", "+00:00").replace(" ", "T", 1)
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        # 生产值班语境默认按北京时间理解，例如 "2026-06-17 13:49:53"。
        dt = dt.replace(tzinfo=timezone(timedelta(hours=8))).astimezone(timezone.utc)
    return dt


def _compute_window(at: str | None, minutes: int) -> tuple[int, int]:
    """算 TLS 查询的 [start_ms, end_ms]。

    - 若给了 at：以 at 为中心，前后各 minutes/2 分钟
    - 若没给 at：以"现在"为终点，往前推 minutes 分钟（传统行为）
    """
    now = datetime.now(tz=timezone.utc)
    center = _parse_at(at) or now
    if _parse_at(at):
        half = max(1, minutes // 2)
        start = int((center.timestamp() - half * 60) * 1000)
        end = int((center.timestamp() + half * 60) * 1000)
    else:
        end = int(now.timestamp() * 1000)
        start = end - minutes * 60 * 1000
    return start, end


def _resolve_real_services(agent_service: str) -> list[str]:
    """把 agent 的别名 service 反向翻译成真实 TLS __container_name__ 列表。

    未映射则原样返回 —— 允许 agent 直接指定 linkcraft-* 走过（虽然不常见）。
    """
    real = _SERVICE_ALIAS_TO_REAL.get(agent_service.strip().lower())
    return real or [agent_service.strip()]


def _build_query(base_query: str, real_services: list[str]) -> str:
    """构造 TLS SearchLogsV2 查询串。

    TLS 语法用 KEY:VALUE 与 AND/OR 组合。字段 __container_name__ 是 K8s pod 容器名，
    对应 prod-linkcraft topic 里各服务的 stdout 来源标签。
    """
    if not real_services:
        return base_query or "*"
    svc_clause = " OR ".join(f'__container_name__:"{s}"' for s in real_services)
    if not base_query or base_query == "*":
        return f"({svc_clause})"
    # base_query 已含时间/关键词等，直接与 service 段 AND
    return f"({base_query}) AND ({svc_clause})"


def _normalize_row(raw: dict[str, Any], agent_service: str) -> dict[str, Any]:
    """把一行 TLS 结果适配成 search_logs 归一化格式，同时脱敏 Content/msg。

    - service 字段用 agent 传入的别名（保持 agent 侧视角一致）
    - service_real 字段追溯原始 __container_name__
    - Content 先脱敏，msg 从脱敏后的首行截短（避免 R3 的顺序 bug）
    """
    content_raw = raw.get("Content") or ""
    content_clean = _sanitize(content_raw)
    first_line = content_clean.splitlines()[0].strip() if content_clean else ""
    msg = first_line if len(first_line) <= 200 else first_line[:200] + "…"

    return {
        "ts": raw.get("Time", ""),
        "level": raw.get("Level", ""),
        "trace_id": raw.get("TraceId", ""),
        "service": agent_service,
        "service_real": raw.get("__container_name__", ""),
        "msg": msg,
        "detail": content_clean,
        "status_code": None,
        "method": None,
        "latency_ms": None,
    }


def query_by_trace(trace_id: str, at: str | None, minutes: int) -> list[dict[str, Any]]:
    """按 TraceId 查云端 —— 用于 trace_query fallback。

    Returns:
        归一化的日志行列表；is_enabled() 为假或查询失败则返回空。
    """
    if not is_enabled() or not trace_id.strip():
        return []
    try:
        from volcengine.tls.tls_requests import SearchLogsRequest  # type: ignore

        client = _get_client()
        start_ms, end_ms = _compute_window(at, minutes)
        query = f'TraceId:"{trace_id.strip()}"'
        req = SearchLogsRequest(
            topic_id=_get_topic_id(),
            query=query,
            start_time=start_ms,
            end_time=end_ms,
            limit=_TLS_LIMIT,
            sort="asc",  # 时间线要升序
        )
        resp = client.search_logs_v2(req)
        result = getattr(resp, "search_result", None) or resp
        rows = getattr(result, "logs", None) or []
        # trace 查询不带 service 过滤，无法反向映射到 agent 别名 —— 用 TLS 原始名兜底
        return [_normalize_row(r, r.get("__container_name__", "")) for r in rows]
    except Exception as exc:
        logger.warning(f"[tls_fallback] trace 查询失败 trace_id={trace_id[:12]}... err={exc}")
        return []


def query_by_service_keyword(
    agent_service: str, keyword: str, at: str | None, minutes: int
) -> list[dict[str, Any]]:
    """按 agent 别名 service + 关键词查云端 —— 用于 search_logs fallback。

    Returns:
        归一化的日志行列表；is_enabled() 为假或查询失败则返回空。
    """
    if not is_enabled():
        return []
    try:
        from volcengine.tls.tls_requests import SearchLogsRequest  # type: ignore

        real_services = _resolve_real_services(agent_service)
        # 关键词直接嵌入 query，TLS 支持全文匹配
        base = keyword.strip() if keyword.strip() else "*"
        query = _build_query(base, real_services)
        start_ms, end_ms = _compute_window(at, minutes)

        client = _get_client()
        req = SearchLogsRequest(
            topic_id=_get_topic_id(),
            query=query,
            start_time=start_ms,
            end_time=end_ms,
            limit=_TLS_LIMIT,
            sort="desc",  # keyword 查询用倒序（最新在前）
        )
        resp = client.search_logs_v2(req)
        result = getattr(resp, "search_result", None) or resp
        rows = getattr(result, "logs", None) or []
        return [_normalize_row(r, agent_service) for r in rows]
    except Exception as exc:
        logger.warning(
            f"[tls_fallback] service 查询失败 service={agent_service} kw={keyword[:30]}... err={exc}"
        )
        return []
