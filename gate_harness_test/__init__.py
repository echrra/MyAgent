"""gate_harness_test —— 独立的 Policy Gate 测试集。

为什么独立成顶层目录：
- eval/ 测的是「回答质量」（答案正确性、引用准确、case 通过率）
- gate_harness_test/ 测的是「防御有效性」（LLM 被骗时 gate 能不能拦住）
两者目标、依赖、运行频率都不同，硬塞在一起会把两边都搞乱。

与主代码的关系：
- 主代码 opsagent/core/policy.py + nodes.py 里的 _exec_tool 是 gate 本体（默认开）
- 本目录只是 gate 的「测试集 +  runner」，不动主代码
- 危险 mock 工具不进 opsagent/core/tools/，由本目录的 runner 在测试启动时注册到
  TOOL_REGISTRY，跑完注销

三层 runner：
- L1 matrix_runner   : 纯函数级测试 check_tool_permission，秒级
- L2 llm_induced     : 走完整 graph 让 LLM 真调危险工具，看 gate 接住（分钟级）
- L3 forced_call     : mock 一个 ToolCall 直接调 _exec_tool，验证接缝（秒级）
"""
