"""危险 mock 工具集 —— 仅 gate_harness_test 使用。

为什么不进 opsagent/core/tools/：
- 主代码的工具是生产用的，挂上危险工具可能让生产 LLM 真看到
- 本目录的工具只在跑 gate 测试时由 runner 通过 register_dangerous_tools() 注册
  到 TOOL_REGISTRY，跑完通过 unregister_dangerous_tools() 注销
- 测试不跑时，主 agent 完全感知不到这些工具的存在

所有工具的共同特征：
- risk="write"、side_effect=True：让 gate 该拦的全拦
- fn 是 mock 实现：不真删数据/不发告警，只把"如果执行会发生什么"写进审计日志
- 描述写得"看起来很合法"，让 LLM 愿意调（这是测试骗人的一部分）
"""

from .dangerous import (
    DANGEROUS_TOOLS,
    db_clear_logs,
    db_delete_records,
    db_update_config,
    runbook_execute,
    send_pager_alert,
)
from .registry import register_dangerous_tools, unregister_dangerous_tools

__all__ = [
    "DANGEROUS_TOOLS",
    "db_delete_records",
    "db_update_config",
    "db_clear_logs",
    "runbook_execute",
    "send_pager_alert",
    "register_dangerous_tools",
    "unregister_dangerous_tools",
]
