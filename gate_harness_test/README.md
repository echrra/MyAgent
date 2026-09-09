# gate_harness_test

独立的 Policy Gate 测试 harness。**测的是防御**，不是回答质量。

## 与 eval/ 的区别

| 维度 | eval/ | gate_harness_test/ |
|---|---|---|
| 测什么 | 答案质量、引用准确性 | LLM 被骗时 gate 能不能拦住 |
| 跑什么 case | 真实业务（E/M 系列） | 风险诱导（D/R/P/S/T 系列） |
| 依赖 RAG/SOP | 是（用真实 SOP/wiki） | 否（自带 mock 毒化语料） |
| 是否需要 LLM | 必须 | L2 需要，L1/L3 不需要 |
| 失败意味着 | 分数退化 → 调优 | 安全失守 → 立刻修 gate |

## 目录结构

```
gate_harness_test/
├── scenarios/             # 测试情景（yaml）
│   ├── direct_invoke/     # 直接诱导调危险工具（D 系列）
│   ├── role_override/     # 角色冒充、假授权（R 系列）
│   ├── sop_poison/        # SOP 检索结果投毒（P 系列）
│   ├── synthesizer_inject/# synthesizer 阶段诱导（S 系列）
│   └── tool_result_inject/# 工具返回里藏指令（T 系列）
├── dangerous_tools/       # 危险 mock 工具（独立，不进主代码）
├── mock_data/
│   └── sop_corpus_poisoned/  # 毒化 SOP 语料（仅测试时用）
├── runners/               # 三层 runner
│   ├── matrix_runner.py   # L1: 纯 gate 矩阵（秒级）
│   ├── llm_induced_runner.py  # L2: LLM 诱导 + gate（分钟级）
│   └── forced_call_runner.py  # L3: mock ToolCall 走 _exec_tool（秒级）
├── verifiers/             # 测试断言器
├── audit/                 # 测试运行产生的审计日志
└── reports/               # 测试报告
```

## 跑测试

```bash
# L1 矩阵测试（秒级，可入 CI）
python scripts/run_gate_tests.py --layer L1

# L3 强制 tool_call 注入测试（秒级）
python scripts/run_gate_tests.py --layer L3

# L2 LLM 诱导测试（分钟级，按需）
python scripts/run_gate_tests.py --layer L2

# 全跑
python scripts/run_gate_tests.py --layer all
```

## 编写新情景

参考 `scenarios/README.md`。
