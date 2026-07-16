"""W3 工具抽象层单测 —— 校验 / 超时 / 重试 / 截断 / 契约。

全部用桩函数（stub），不依赖 DB / 模型 / 真实数据，CI 必跑。
"""

import time

import pytest

from opsagent.core.tools.base import (
    Tool,
    ToolTimeoutError,
    ToolValidationError,
    _truncate,
)

# ---------------- 入参校验 ----------------


def _make_args_model():
    """造一个最小入参模型：service 必填、minutes 可选默认 10。"""
    from pydantic import BaseModel, Field

    class _Args(BaseModel):
        service: str
        minutes: int = Field(default=10)

    return _Args


def test_validation_rejects_bad_args_without_calling_fn():
    """缺必填 / 类型错 → 抛 ToolValidationError，且原 fn 一次都没被调用。"""
    calls = {"n": 0}

    def fn(**kwargs):
        calls["n"] += 1
        return {"data": kwargs, "meta": {}}

    tool = Tool(name="t", fn=fn, args_model=_make_args_model())

    with pytest.raises(ToolValidationError):
        tool()  # 缺 service
    with pytest.raises(ToolValidationError):
        tool(service="svc", minutes="not-int")  # 类型错

    assert calls["n"] == 0, "校验未过却调用了真实函数"


def test_validation_applies_defaults():
    """省略可选参数 → 用 Pydantic 默认值填充后正常执行。"""

    def fn(service, minutes):
        return {"data": {"service": service, "minutes": minutes}, "meta": {}}

    tool = Tool(name="t", fn=fn, args_model=_make_args_model())
    out = tool(service="svc")
    assert out["data"] == {"service": "svc", "minutes": 10}


def test_no_args_model_passthrough():
    """无入参模型 → kwargs 原样透传，不校验。"""

    def fn(**kwargs):
        return {"data": kwargs, "meta": {}}

    tool = Tool(name="t", fn=fn, args_model=None)
    out = tool(anything=1, foo="bar")
    assert out["data"] == {"anything": 1, "foo": "bar"}


# ---------------- 超时 ----------------


def test_timeout_raises():
    """执行超过 timeout_s → 抛 ToolTimeoutError。"""

    def slow(**_):
        time.sleep(0.5)
        return {"data": [], "meta": {}}

    tool = Tool(name="slow", fn=slow, args_model=None, timeout_s=0.1, max_retries=0)
    with pytest.raises(ToolTimeoutError):
        tool()


# ---------------- 重试 ----------------


def test_retry_succeeds_after_one_failure():
    """先失败一次再成功 → 最终成功（max_retries=1 即额外重试 1 次）。"""
    state = {"n": 0}

    def flaky(**_):
        state["n"] += 1
        if state["n"] == 1:
            raise RuntimeError("第一次故意失败")
        return {"data": "ok", "meta": {}}

    tool = Tool(name="flaky", fn=flaky, args_model=None, max_retries=1)
    out = tool()
    assert out["data"] == "ok"
    assert state["n"] == 2, "应在第二次尝试成功"


def test_retry_exhausted_reraises():
    """恒失败 → 重试用尽后原样抛出最后的异常（交给 tool_exec 记 success=False）。"""
    state = {"n": 0}

    def always_fail(**_):
        state["n"] += 1
        raise ValueError("永远失败")

    tool = Tool(name="bad", fn=always_fail, args_model=None, max_retries=1)
    with pytest.raises(ValueError, match="永远失败"):
        tool()
    assert state["n"] == 2, "应尝试 max_retries+1=2 次"


# ---------------- 输出截断 ----------------


def test_truncate_helper_caps_str_and_list():
    """_truncate：长字符串截断、长列表截断并标记发生过截断。"""
    obj = {"s": "x" * 100, "lst": list(range(50))}
    out, cut = _truncate(obj, max_str=10, max_items=5)
    assert cut is True
    assert out["s"].startswith("x" * 10) and "截断" in out["s"]
    # 5 条保留 + 1 条省略提示
    assert len(out["lst"]) == 6
    assert "省略" in out["lst"][-1]


def test_large_output_truncated_and_flagged():
    """工具吐回超大 data → 被截断且 meta.truncated=True。"""

    def fn(**_):
        return {"data": {"text": "y" * 5000}, "meta": {"k": "v"}}

    tool = Tool(name="big", fn=fn, args_model=None, max_str_chars=100)
    out = tool()
    assert len(out["data"]["text"]) < 5000
    assert out["meta"]["truncated"] is True
    assert out["meta"]["k"] == "v", "原 meta 字段应保留"


def test_small_output_not_flagged():
    """小 data → 原样透传，不打 truncated 标记。"""

    def fn(**_):
        return {"data": {"text": "short"}, "meta": {}}

    tool = Tool(name="small", fn=fn, args_model=None)
    out = tool()
    assert out["data"]["text"] == "short"
    assert "truncated" not in out["meta"]


# ---------------- 契约 ----------------


def test_callable_returns_data_meta_dict():
    """Tool 实例可 (**kwargs) 调用，返回 {data, meta}，与裸函数行为一致。"""

    def fn(service):
        return {"data": [1, 2, 3], "meta": {"service": service}}

    tool = Tool(name="t", fn=fn, args_model=None)
    out = tool(service="svc")
    assert set(out.keys()) >= {"data", "meta"}
    assert out["data"] == [1, 2, 3]
    assert out["meta"]["service"] == "svc"
