"""W3 第二块 FC 工具单测 —— trace_query / create_ticket / change_query。

- create_ticket / change_query 是纯 mock，无外部依赖，CI 必跑。
- trace_query 走真实合成日志（data/logs/synth，随 W0 提交）；若日志缺失则 skip，
  避免缺数据时假阳性失败。统一经 TOOL_REGISTRY 调用，顺带验证 base.Tool 的
  「入参校验 + 截断」整条包装路径。
"""

import pytest

from opsagent.core.tools import TOOL_DESCRIPTIONS, TOOL_REGISTRY
from opsagent.core.tools.base import ToolValidationError
from opsagent.core.tools.mock import change_query, create_ticket

# ---------------- create_ticket ----------------


def test_create_ticket_structure_and_status():
    out = create_ticket(service="edgectl-backend-http", title="P99 突增")
    data = out["data"]
    assert data["status"] == "created"
    assert data["ticket_id"].startswith("INC-")
    assert data["service"] == "edgectl-backend-http"
    assert out["meta"]["mock"] is True


def test_create_ticket_id_is_deterministic():
    """同 (service, title) → 同工单号；不同 → 不同。"""
    a = create_ticket(service="svc", title="同一个问题")["data"]["ticket_id"]
    b = create_ticket(service="svc", title="同一个问题")["data"]["ticket_id"]
    c = create_ticket(service="svc", title="另一个问题")["data"]["ticket_id"]
    assert a == b
    assert a != c


# ---------------- change_query ----------------


def test_change_query_returns_list():
    out = change_query()
    assert isinstance(out["data"], list)
    assert len(out["data"]) >= 1
    assert out["meta"]["mock"] is True


def test_change_query_filters_by_service():
    out = change_query(service="edgectl-admin")
    assert all(c["service"] == "edgectl-admin" for c in out["data"])
    # 不存在的服务 → 空
    assert change_query(service="not-exist")["data"] == []


# ---------------- trace_query（经 registry，含校验/截断包装）----------------


def _trace_tool():
    return TOOL_REGISTRY["trace_query"]


def test_trace_query_requires_at_least_one_arg():
    """trace_id / service 都为空 → at-least-one 校验失败 → ToolValidationError。"""
    with pytest.raises(ToolValidationError):
        _trace_tool()(trace_id="", service="")


def test_trace_query_by_service_lists_traces():
    out = _trace_tool()(service="edgectl-backend-http")
    traces = out["data"]["traces"]
    if not traces:
        pytest.skip("合成日志为空或该服务无链路，跳过")
    assert out["meta"]["mode"] == "by_service"
    assert out["meta"]["derived_from"] == "logs_by_trace_id"
    first = traces[0]
    assert {"trace_id", "has_error", "max_latency_ms", "services"} <= set(first)


def test_trace_query_by_trace_id_returns_timeline():
    """先按服务拿一个真实 trace_id，再下钻验证时间线 + summary。"""
    seed = _trace_tool()(service="edgectl-backend-http")["data"]["traces"]
    if not seed:
        pytest.skip("合成日志为空，跳过")
    tid = seed[0]["trace_id"]

    out = _trace_tool()(trace_id=tid)
    assert out["meta"]["mode"] == "by_trace"
    data = out["data"]
    assert data["trace_id"] == tid
    assert len(data["timeline"]) >= 1
    summary = data["summary"]
    assert {"services", "n_spans", "error_count", "max_latency_ms"} <= set(summary)
    # summary 在函数内基于完整数据算；timeline 可能被 base.Tool 按 max_list_items 截断，
    # 故真实 n_spans ≥ 返回的时间线行数（截断后末尾可能多一条「省略」提示字符串）。
    dict_rows = [x for x in data["timeline"] if isinstance(x, dict)]
    assert summary["n_spans"] >= len(dict_rows) >= 1


# ---------------- 注册一致性 ----------------


def test_new_tools_registered_and_described():
    for name in ("trace_query", "create_ticket", "change_query"):
        assert name in TOOL_REGISTRY, f"{name} 未注册到 TOOL_REGISTRY"
        assert name in TOOL_DESCRIPTIONS, f"{name} 未出现在 TOOL_DESCRIPTIONS"
