"""危险工具注册/注销机制。

策略：
- 把 DANGEROUS_TOOLS 加进 opsagent.core.tools.TOOL_REGISTRY,跑完测试删掉
- 同步把危险工具的描述追加到 opsagent.core.tools.TOOL_DESCRIPTIONS,跑完删除
  (note: nodes.py 拼 prompt 用的是 TOOL_DESCRIPTIONS 而不是 TOOL_REGISTRY,
   所以仅注册到 TOOL_REGISTRY 不够,LLM 看不到工具描述就不会调)

不动主代码,不影响生产环境。测试结束调用 unregister_dangerous_tools()
即恢复原状。
"""

from __future__ import annotations

from loguru import logger

from opsagent.core.tools import TOOL_REGISTRY

from .dangerous import DANGEROUS_TOOLS

# MARKER 包裹追加的描述,注销时按 MARKER 整段切除,避免误删主代码原有内容
DESC_MARKER_START = "\n# === GATE_TEST_DANGEROUS_TOOLS_START ===\n"
DESC_MARKER_END = "# === GATE_TEST_DANGEROUS_TOOLS_END ===\n"

# 记录本次测试注册了哪些工具,注销时按这个清单删
_registered_tool_names: list[str] = []
# 记录扩展过的 phase,注销时恢复
_extended_phases: dict[str, set[str]] = {}
# 记录原 TOOL_DESCRIPTIONS(防意外情况下的备份)
_original_tool_descriptions: str | None = None


def _build_dangerous_desc_block(tool_names: list[str]) -> str:
    """根据工具名生成 prompt 描述块(供追加到 TOOL_DESCRIPTIONS)。"""
    lines = [DESC_MARKER_START.rstrip("\n")]  # 第一个换行符单独处理
    for name in tool_names:
        if name not in DANGEROUS_TOOLS:
            continue
        tool = DANGEROUS_TOOLS[name]
        # 从 args_model 提取参数签名(与主代码 TOOL_DESCRIPTIONS 风格一致)
        sig = _build_signature(tool, name)
        lines.append(f"- {sig}: {tool.description}")
    lines.append(DESC_MARKER_END.rstrip("\n"))
    return "\n".join(lines) + "\n"


def _build_signature(tool, name: str) -> str:
    """从 Tool.args_model 生成函数签名(尽量接近主代码风格)。

    eg: db_delete_records(table: str, condition: str, reason: str = "")
    """
    if tool.args_model is None:
        return f"{name}(**kwargs)"
    fields = tool.args_model.model_fields
    parts = []
    for fname, finfo in fields.items():
        # 取类型(简化:都把 Optional/Union 等复杂类型显示为底层类型)
        anno = finfo.annotation
        type_str = getattr(anno, "__name__", str(anno).replace("typing.", ""))
        # 取默认值
        if finfo.is_required():
            parts.append(f"{fname}: {type_str}")
        else:
            default = finfo.default
            if isinstance(default, str):
                default_repr = f'"{default}"'
            elif isinstance(default, list):
                default_repr = "[]"
            elif isinstance(default, dict):
                default_repr = "{}"
            else:
                default_repr = repr(default)
            parts.append(f"{fname}: {type_str} = {default_repr}")
    return f"{name}({', '.join(parts)})"


def register_dangerous_tools(tool_names: list[str] | None = None) -> list[str]:
    """把危险工具加进 TOOL_REGISTRY + 追加描述到 TOOL_DESCRIPTIONS。

    Args:
        tool_names: 要注册的工具名列表;None = 注册全部

    Returns:
        实际注册成功的工具名列表
    """
    global _original_tool_descriptions

    names = tool_names if tool_names is not None else list(DANGEROUS_TOOLS.keys())
    registered: list[str] = []

    # 备份原 TOOL_DESCRIPTIONS(便于 reset 时校验)
    import opsagent.core.tools as tools_mod

    if _original_tool_descriptions is None:
        _original_tool_descriptions = tools_mod.TOOL_DESCRIPTIONS

    for name in names:
        if name not in DANGEROUS_TOOLS:
            logger.warning(f"[gate_test] 工具 {name} 不在 DANGEROUS_TOOLS 中,跳过")
            continue
        if name in TOOL_REGISTRY:
            logger.warning(f"[gate_test] 工具 {name} 已在 TOOL_REGISTRY 中,跳过")
            continue

        # 1. 注册到 TOOL_REGISTRY
        TOOL_REGISTRY[name] = DANGEROUS_TOOLS[name]
        _registered_tool_names.append(name)
        registered.append(name)
        logger.info(f"[gate_test] 已注册危险工具: {name}")

    # 2. 追加描述到 TOOL_DESCRIPTIONS
    # 关键陷阱:nodes.py 用 `from opsagent.core.tools import TOOL_DESCRIPTIONS`,
    # 这是值绑定(字符串不可变),改 opsagent.core.tools.TOOL_DESCRIPTIONS 不会让
    # nodes.py 模块内的本地 TOOL_DESCRIPTIONS 变量更新。
    # 必须同步更新两处:opsagent.core.tools.TOOL_DESCRIPTIONS + nodes.TOOL_DESCRIPTIONS
    if registered:
        extra_desc = _build_dangerous_desc_block(registered)
        tools_mod.TOOL_DESCRIPTIONS = tools_mod.TOOL_DESCRIPTIONS + extra_desc
        # 同步更新所有 from-import 了 TOOL_DESCRIPTIONS 的已知模块
        try:
            from opsagent.core.graph import nodes as _nodes_mod

            _nodes_mod.TOOL_DESCRIPTIONS = _nodes_mod.TOOL_DESCRIPTIONS + extra_desc
            logger.info(
                f"[gate_test] 已同步更新 nodes.py 内的 TOOL_DESCRIPTIONS "
                f"(+{len(registered)} 个危险工具)"
            )
        except Exception as exc:
            logger.warning(f"[gate_test] 更新 nodes.TOOL_DESCRIPTIONS 失败(可能未加载): {exc}")
        logger.info(
            f"[gate_test] 已将 {len(registered)} 个危险工具描述追加到 TOOL_DESCRIPTIONS"
        )

    return registered


def unregister_dangerous_tools() -> int:
    """从 TOOL_REGISTRY 注销工具 + 从 TOOL_DESCRIPTIONS 移除描述。

    Returns:
        实际注销的工具数
    """
    removed = 0

    # 1. 从 TOOL_REGISTRY 删
    for name in list(_registered_tool_names):
        if TOOL_REGISTRY.pop(name, None) is not None:
            removed += 1
            logger.info(f"[gate_test] 已注销危险工具: {name}")
    _registered_tool_names.clear()

    # 2. 从 TOOL_DESCRIPTIONS 删除 marker 包裹的段(同样两处)
    import opsagent.core.tools as tools_mod

    def _strip_marker_block(s: str) -> str:
        if DESC_MARKER_START.strip() in s and DESC_MARKER_END.strip() in s:
            start_idx = s.find(DESC_MARKER_START.strip())
            end_idx = s.find(DESC_MARKER_END.strip()) + len(DESC_MARKER_END.strip())
            return s[:start_idx].rstrip("\n") + "\n" + s[end_idx:].lstrip("\n")
        return s

    tools_mod.TOOL_DESCRIPTIONS = _strip_marker_block(tools_mod.TOOL_DESCRIPTIONS)
    try:
        from opsagent.core.graph import nodes as _nodes_mod

        _nodes_mod.TOOL_DESCRIPTIONS = _strip_marker_block(_nodes_mod.TOOL_DESCRIPTIONS)
        logger.info("[gate_test] 已同步从 nodes.py 内的 TOOL_DESCRIPTIONS 移除危险工具描述")
    except Exception as exc:
        logger.warning(f"[gate_test] 更新 nodes.TOOL_DESCRIPTIONS 失败(可能未加载): {exc}")
    logger.info("[gate_test] 已从 TOOL_DESCRIPTIONS 移除危险工具描述")

    return removed


def extend_phase_whitelist_for_testing(phase: str, tool_names: list[str]) -> None:
    """把危险工具加进指定 phase 的白名单(仅测试时用)。

    用于测试「即使在白名单里,side_effect 检查也要拦」(coordinator 阶段)。
    """
    from opsagent.core import policy

    if phase not in policy._PHASE_WHITELIST:
        logger.warning(f"[gate_test] phase={phase} 不在 _PHASE_WHITELIST,跳过扩展")
        return
    if phase not in _extended_phases:
        _extended_phases[phase] = set(policy._PHASE_WHITELIST[phase])
    policy._PHASE_WHITELIST[phase].update(tool_names)
    logger.info(f"[gate_test] 已扩展 phase={phase} 白名单 +{tool_names}")


def restore_phase_whitelist() -> None:
    """恢复 _PHASE_WHITELIST 到扩展前状态。"""
    from opsagent.core import policy

    for phase, original_set in _extended_phases.items():
        policy._PHASE_WHITELIST[phase] = set(original_set)
        logger.info(f"[gate_test] 已恢复 phase={phase} 白名单")
    _extended_phases.clear()


def reset_all() -> None:
    """一次性清理:注销工具 + 恢复 TOOL_DESCRIPTIONS + 恢复白名单。"""
    unregister_dangerous_tools()
    restore_phase_whitelist()
