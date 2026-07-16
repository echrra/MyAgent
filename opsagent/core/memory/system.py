"""L1 System 层 —— 动态拼装 System Prompt。

docs/04 §2.1：System 不是静态字符串，要按「当前可用工具」+「用户画像角色」动态拼装。
- 工具列表：直接用 tools 聚合层维护的 TOOL_DESCRIPTIONS（双轨 FC + MCP 共 8 个）
- 角色语气：按 Profile（L2）的 role 调整措辞（SRE 可用术语 / 新人避行话 / Manager 偏结论）

本模块无 DB / 模型依赖，纯字符串拼装 —— **必定成功**，是 4 层里唯一不会降级的一层。
role 来自 L2 Profile，W4-A 阶段 load_memory 暂传 None（基线语气），W4-D 接通 Profile 后传实值。
"""

from __future__ import annotations

from opsagent.core.prompts import load as load_prompt

# 角色 → 一句话语气提示（docs/04 §2.1 示例）。键统一小写匹配，未知角色走基线（空串）。
_ROLE_TONE: dict[str, str] = {
    "sre": "- 用户具备 SRE 背景，可使用专业术语与缩写（P99/QPS/熔断/退避等），直接给排查路径。\n",
    "backend": "- 用户是业务后端工程师，可用一定技术术语，但中间件/SRE 专有名词酌情点一句。\n",
    "newbie": "- 用户是新人，尽量避免行话；必须用的术语后附一句通俗解释。\n",
    "manager": "- 用户偏管理视角，先讲业务影响面与结论，技术细节其次、可折叠。\n",
}


def _role_tone(role: str | None) -> str:
    """把 role 映射成 system prompt 里的语气段；未知/None → 基线（无附加）。"""
    if not role:
        return ""
    return _ROLE_TONE.get(role.strip().lower(), "")


def build_system_prompt(tool_descriptions: str, role: str | None = None) -> str:
    """拼装 L1 System Prompt：角色 + 能力（工具列表）+ 安全约束（含角色语气）+ 输出格式。

    Args:
        tool_descriptions: 当前可用工具说明（来自 tools 聚合层 TOOL_DESCRIPTIONS）。
        role: 用户画像角色（来自 L2 Profile）；None 时走基线语气。
    """
    return load_prompt("system").format(
        tool_descriptions=tool_descriptions,
        role_tone=_role_tone(role),
    )
