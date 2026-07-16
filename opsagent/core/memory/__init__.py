"""4 层自研记忆系统（docs/04）。

分层与落点：
- L1 System   : system.py —— 动态拼装 System Prompt（无 DB，必成）
- L2 Profile  : profile.py —— 用户画像 + 历史故障模式（W4-D 接入）
- L3 Episodic : episodic.py / compactor.py —— 对话历史 + 结构化压缩（W4-B/C 接入）
- L4 Working  : working.py —— 本轮工具记忆写入时压缩

store.py 是 L2/L3 共用的纯 SQL 存取层（复用 retrieval 的 PG 连接，靠表名隔离）。
W4-A 仅落地 L1 / L4 / 存储地基；L2/L3 业务模块随 B/C/D 增量接入。
"""

from opsagent.core.memory.compactor import (
    compact_session,
    count_tokens,
    resolve_max_context,
    should_compact,
)
from opsagent.core.memory.episodic import (
    EpisodicContext,
    load_episodic,
    persist_turn,
    render_history,
)
from opsagent.core.memory.profile import (
    ProfileContext,
    load_profile,
    run_profile_updater,
)
from opsagent.core.memory.system import build_system_prompt
from opsagent.core.memory.working import (
    compact_record_for_prompt,
    compact_working_memory,
)

__all__ = [
    "EpisodicContext",
    "ProfileContext",
    "build_system_prompt",
    "compact_record_for_prompt",
    "compact_session",
    "compact_working_memory",
    "count_tokens",
    "load_episodic",
    "load_profile",
    "persist_turn",
    "render_history",
    "resolve_max_context",
    "run_profile_updater",
    "should_compact",
]
