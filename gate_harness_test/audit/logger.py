"""审计日志写入 + 读取。

实现思路：
- 模块级 _current_session 持有当前 audit session 状态（run_id + 文件路径）
- start_audit_session(run_id) 创建 gate_harness_test/audit/{run_id}/ 目录，初始化三个 jsonl
- end_audit_session() 关闭 session，返回三个文件的路径
- record_decision / record_tool_call / record_dangerous_exec 在 session 活跃时追加写一行

为什么用模块级状态：
- 跨多个模块（policy/nodes/dangerous_tools）都需要写审计，但不想每处都传 run_id
- 测试是串行跑的，全局一个 session 够用
- 没有 session 时,record_* 是 no-op（不报错）
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from loguru import logger

AUDIT_ROOT = Path(__file__).resolve().parent / "runs"

_current_session: dict[str, Any] | None = None


def start_audit_session(run_id: str) -> Path:
    """开启一个审计 session，返回审计目录路径。"""
    global _current_session
    if _current_session is not None:
        logger.warning(
            f"[audit] 已有活跃 session run_id={_current_session['run_id']}，"
            "新 session 会覆盖它"
        )
    audit_dir = AUDIT_ROOT / run_id
    audit_dir.mkdir(parents=True, exist_ok=True)
    # 初始化三个文件（空）
    (audit_dir / "decisions.jsonl").touch()
    (audit_dir / "tool_calls.jsonl").touch()
    (audit_dir / "dangerous_exec.jsonl").touch()

    _current_session = {
        "run_id": run_id,
        "audit_dir": audit_dir,
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    logger.info(f"[audit] session 开启: {audit_dir}")
    return audit_dir


def end_audit_session() -> dict[str, Any]:
    """关闭 session，返回 session 摘要。"""
    global _current_session
    if _current_session is None:
        logger.warning("[audit] 没有活跃 session 可关闭")
        return {}
    summary = {
        "run_id": _current_session["run_id"],
        "audit_dir": str(_current_session["audit_dir"]),
        "started_at": _current_session["started_at"],
        "ended_at": datetime.now(timezone.utc).isoformat(),
    }
    logger.info(f"[audit] session 关闭: {summary['run_id']}")
    _current_session = None
    return summary


def _append(filename: str, record: dict[str, Any]) -> None:
    """往当前 session 的某个 jsonl 文件追加一行。"""
    if _current_session is None:
        # 没有 session：静默跳过（可能是非测试场景下被调到）
        return
    path = _current_session["audit_dir"] / filename
    record = dict(record)
    record["ts"] = time.time()
    record["ts_iso"] = datetime.now(timezone.utc).isoformat()
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


def record_decision(
    phase: str,
    tool_name: str,
    decision: str,
    reason: str,
    risk: str = "",
    side_effect: bool | None = None,
    scenario_id: str = "",
) -> None:
    """记录一次 gate 决策。

    Args:
        phase: 决策发生的阶段（coordinator / worker / synthesizer / ...）
        tool_name: 工具名
        decision: "allow" / "deny"
        reason: 拒绝理由（allow 时为空串）
        risk: 工具风险等级（从 Tool 实例读）
        side_effect: 工具是否有副作用
        scenario_id: 当前正在跑的 scenario id（如 D01）
    """
    _append(
        "decisions.jsonl",
        {
            "kind": "gate_decision",
            "scenario_id": scenario_id,
            "phase": phase,
            "tool_name": tool_name,
            "decision": decision,
            "reason": reason,
            "risk": risk,
            "side_effect": side_effect,
        },
    )


def record_tool_call(
    tool_name: str,
    args: dict[str, Any],
    phase: str,
    scenario_id: str = "",
) -> None:
    """记录一次工具调用尝试（无论是否被 gate 拦截）。"""
    _append(
        "tool_calls.jsonl",
        {
            "kind": "tool_call",
            "scenario_id": scenario_id,
            "phase": phase,
            "tool_name": tool_name,
            "args": args,
        },
    )


def record_dangerous_exec(
    tool_name: str,
    args: dict[str, Any],
    would_impact: str,
) -> None:
    """记录一次危险工具的 actually 执行（mock fn 被调到）。

    关键区分：
    - gate 拦住时 fn 不会被调，此函数不会被调到
    - gate 没拦住时 fn 会被调，此函数会被调到 → dangerous_exec.jsonl 会非空
    """
    _append(
        "dangerous_exec.jsonl",
        {
            "kind": "dangerous_exec",
            "tool_name": tool_name,
            "args": args,
            "would_impact": would_impact,
        },
    )
    # 同时打到 console，跑测试时能立刻看到
    logger.warning(
        f"[audit] ⚠️ 危险工具 actually 执行: {tool_name} args={args} 影响={would_impact}"
    )


def read_audit_logs(run_id: str) -> dict[str, list[dict[str, Any]]]:
    """读取指定 run_id 的所有审计日志。"""
    audit_dir = AUDIT_ROOT / run_id
    if not audit_dir.is_dir():
        return {"decisions": [], "tool_calls": [], "dangerous_exec": []}

    def _read(name: str) -> list[dict[str, Any]]:
        path = audit_dir / name
        if not path.exists():
            return []
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    return {
        "decisions": _read("decisions.jsonl"),
        "tool_calls": _read("tool_calls.jsonl"),
        "dangerous_exec": _read("dangerous_exec.jsonl"),
    }
