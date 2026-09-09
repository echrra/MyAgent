"""在 policy.check_tool_permission 外面包一层，把决策写进 audit。

不动主代码 policy.py；测试启动时由 runner 调用 install_decision_hook()，
跑完 uninstall_decision_hook() 恢复原函数。

为什么要 hook 而不是改 policy.py：
- policy.py 是主代码，审计是测试专用副作用，不该耦合进去
- 主流程（生产环境）跑时，hook 未安装,record_decision 是 no-op，零开销
- 测试时 hook 装上,所有 decision 都进 audit
"""

from __future__ import annotations

from typing import Any

from loguru import logger

from . import logger as audit_logger

# 保存原函数,uninstall 时恢复
_original_check_tool_permission = None
_current_scenario_id: str = ""


def set_current_scenario(scenario_id: str) -> None:
    """记录当前正在跑的 scenario id，让 decision 能归属到具体 case。"""
    global _current_scenario_id
    _current_scenario_id = scenario_id


def clear_current_scenario() -> None:
    """清理 scenario id。"""
    global _current_scenario_id
    _current_scenario_id = ""


def install_decision_hook() -> None:
    """把 policy.check_tool_permission 替换为带审计版。"""
    global _original_check_tool_permission
    if _original_check_tool_permission is not None:
        logger.warning("[audit hook] hook 已安装，跳过重复安装")
        return

    from opsagent.core import policy

    _original_check_tool_permission = policy.check_tool_permission

    def hooked_check_tool_permission(
        phase: str,
        tool_name: str,
        tool: Any = None,
        *,
        enabled: bool = True,
    ):
        allowed, reason = _original_check_tool_permission(
            phase, tool_name, tool, enabled=enabled
        )
        # 记录决策
        audit_logger.record_decision(
            phase=phase,
            tool_name=tool_name,
            decision="allow" if allowed else "deny",
            reason=reason,
            risk=tool.risk if tool else "",
            side_effect=tool.side_effect if tool else None,
            scenario_id=_current_scenario_id,
        )
        return allowed, reason

    policy.check_tool_permission = hooked_check_tool_permission
    logger.info("[audit hook] decision hook 已安装")


def uninstall_decision_hook() -> None:
    """恢复原 check_tool_permission。"""
    global _original_check_tool_permission
    if _original_check_tool_permission is None:
        return
    from opsagent.core import policy

    policy.check_tool_permission = _original_check_tool_permission
    _original_check_tool_permission = None
    logger.info("[audit hook] decision hook 已卸载")
