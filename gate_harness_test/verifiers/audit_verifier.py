"""Audit 完整性断言 —— 验证关键事件都有审计记录。

判断标准:
- 若 expect.audit_should_record_decision=true:
    decisions.jsonl 中应有 expected_dangerous_tool 的决策记录
"""

from __future__ import annotations

from typing import Any


def verify_audit_complete(
    decisions_log: list[dict[str, Any]],
    expected_dangerous_tool: str,
    should_record_decision: bool,
) -> tuple[bool, str]:
    """验证 gate 决策是否被记录了。"""
    if not should_record_decision:
        return True, ""

    decision_records = [
        r for r in decisions_log
        if r.get("tool_name") == expected_dangerous_tool
    ]
    if not decision_records:
        return False, (
            f"audit 中没有 {expected_dangerous_tool} 的决策记录,"
            "audit hook 失效或 _exec_tool 未走 gate"
        )
    return True, ""
