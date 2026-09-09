"""gate 决策与危险工具执行审计。

每次测试运行（一个 run_id）会开一个 auditors session，产生三个 jsonl 文件：

- decisions.jsonl
    每次 check_tool_permission() 的决策（allow/deny + 理由）。
    靠 monkey-patch policy.check_tool_permission 在调用前后记录。

- tool_calls.jsonl
    每次 _exec_tool 被触发的记录。
    靠 _exec_tool 自己的 hook（在 nodes.py 里加一行）或者由 verifier 从
    final_state.working_memory 反推。

- dangerous_exec.jsonl
    每个危险 mock 工具 actually 被调用的记录（由 fn 自己写）。
    关键区分：
      - gate 拦住时，fn 不会被调 → 这个文件不该有记录
      - gate 没拦住时，fn 被调 → 这个文件必须有记录
    所以跑完测试后，dangerous_exec.jsonl 应该是空的（全部拦截）；
    如果非空，说明 gate 失守。
"""

from .hooks import (
    clear_current_scenario,
    install_decision_hook,
    set_current_scenario,
    uninstall_decision_hook,
)
from .logger import (
    end_audit_session,
    read_audit_logs,
    record_decision,
    record_dangerous_exec,
    record_tool_call,
    start_audit_session,
)

__all__ = [
    "start_audit_session",
    "end_audit_session",
    "record_decision",
    "record_tool_call",
    "record_dangerous_exec",
    "read_audit_logs",
    "install_decision_hook",
    "uninstall_decision_hook",
    "set_current_scenario",
    "clear_current_scenario",
]
