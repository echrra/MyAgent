"""Mock 工具集 —— 仍未接真实数据的工具留在这里。

W1 现状：
- search_logs / search_sop 已迁到 real.py（接入合成日志 + 知识库）
- get_service_metrics 仍是 mock —— RED 指标数据（§2.5 trace/metric_gen）尚未生成，
  待后续补齐数据后再迁到 real.py

W3 新增（纯 mock，路线图 W3）：
- create_ticket：模拟创建工单，返回确定性 ticket_id（不碰真实工单系统/网络）
- change_query：模拟查询近期变更（发布/配置/扩缩容），剧情与合成故障呼应

设计：返回结构与真实工具一致（{"data": ..., "meta": ...}），便于无痛切换。
"""

import hashlib
import time
from typing import Any


def get_service_metrics(service: str, minutes: int = 5) -> dict[str, Any]:
    """Mock：拿服务最近 N 分钟的 RED 指标。

    指标数据尚未合成，暂返回固定的"明显异常"样本，供 Plan/Answer 链路联调。
    """
    time.sleep(0.05)  # 模拟 IO
    return {
        "data": {
            "rate_qps": 132.4,
            "error_rate": 0.18,  # 18% 错误率，明显异常
            "p99_latency_ms": 4820,
            "p50_latency_ms": 156,
        },
        "meta": {"service": service, "minutes": minutes, "mock": True},
    }


# ====================== create_ticket（mock 工单创建）======================


def create_ticket(
    service: str, title: str, severity: str = "P2", description: str = ""
) -> dict[str, Any]:
    """Mock：创建一张运维工单，返回工单号。

    纯本地 mock：不调用任何真实工单/网络系统，无副作用。ticket_id 由
    md5(service|title) 前 6 位 hex 派生 —— 确定性、可复现、可单测，
    同一 (service, title) 永远得到同一工单号（也利于后续 eval 重放）。

    Args:
        service: 关联服务名
        title: 工单标题（一句话问题概述）
        severity: 严重级别（P0/P1/P2/P3），默认 P2
        description: 详细描述（可选）

    Returns:
        {"data": {ticket_id, status, service, severity, url}, "meta": {"mock": True}}
    """
    digest = hashlib.md5(f"{service}|{title}".encode()).hexdigest()[:6]
    ticket_id = f"INC-{digest}"
    return {
        "data": {
            "ticket_id": ticket_id,
            "status": "created",
            "service": service,
            "severity": severity,
            "title": title,
            "url": f"https://ticket.example.local/incidents/{ticket_id}",
        },
        "meta": {"mock": True, "has_description": bool(description)},
    }


# ====================== change_query（mock 变更查询）======================

# 静态变更表：按服务编排近期变更剧情，与合成故障呼应（如「刚发版」常是根因）。
# ts 为相对锚点的偏移描述，避免硬编码绝对时间；真实接入时由变更系统返回。
_MOCK_CHANGES: list[dict[str, Any]] = [
    {
        "change_id": "CHG-1001",
        "type": "deploy",
        "service": "edgectl-backend-http",
        "ts": "2026-06-08T09:52:00+00:00",
        "summary": "edgectl-backend-http 发布 v1.8.3（含连接池参数调整）",
        "operator": "deploy-bot",
    },
    {
        "change_id": "CHG-1002",
        "type": "config",
        "service": "edgectl-backend-watcher",
        "ts": "2026-06-08T09:40:00+00:00",
        "summary": "edgectl-backend-watcher 定时任务并发度 4→16",
        "operator": "ops-li",
    },
    {
        "change_id": "CHG-1003",
        "type": "scale",
        "service": "edgectl-admin",
        "ts": "2026-06-08T08:30:00+00:00",
        "summary": "edgectl-admin 副本数 2→3",
        "operator": "hpa",
    },
]


def change_query(service: str = "", minutes: int = 60) -> dict[str, Any]:
    """Mock：查询近期变更事件（发布 / 配置 / 扩缩容）。

    纯本地 mock：返回内置静态变更表；给 service 则过滤到该服务。
    变更与近期故障常存在因果（「发版即出事」），是排障必查的一环。

    Args:
        service: 服务名；为空则返回全部服务的变更
        minutes: 时间窗（分钟）。mock 数据时间固定，仅记入 meta 不实际过滤。

    Returns:
        {"data": [变更...], "meta": {"mock": True, ...}}（按时间倒序，最新在前）。
    """
    svc = service.strip().lower()
    hits = [c for c in _MOCK_CHANGES if not svc or c["service"].lower() == svc]
    # 最新变更在前
    hits = sorted(hits, key=lambda c: c["ts"], reverse=True)
    return {
        "data": hits,
        "meta": {
            "mock": True,
            "service": service,
            "minutes": minutes,
            "returned": len(hits),
        },
    }

