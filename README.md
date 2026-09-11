# OpsAgent

智能运维诊断 Agent —— 基于假设驱动的多 Agent 并行故障定位系统，内建 Wiki 图谱（RAG 增强）与阶段化工具权限门（Policy Gate）。

## 简介

OpsAgent 是一个面向微服务架构的智能运维诊断工具，通过 Multi-Agent 协作实现自动化故障根因分析。输入一段故障描述或告警信息，OpsAgent 会自动生成故障假设、并行调度工具验证、综合证据给出带引用的诊断结论。

**核心能力**：
- **假设驱动并行诊断**：Coordinator 生成多故障假设，Worker 并行验证，Synthesizer 综合证据
- **混合检索 RAG + Wiki 图谱**：pgvector 向量检索 + BM25 全文检索 + RRF 融合 + Rerank 精排；`wiki_graph.py` 把 KB 文档间手写交叉引用解析成有向图，作为「RAG 增强」叠加在召回之上——不改既有 retrieval 主链路，命中后用 `wiki_read` 工具顺着链接做下一跳扩展
- **Policy Gate 工具权限门**：按 LangGraph 节点（coordinator / worker / synthesizer 等）白名单硬拦越权工具调用；`create_ticket` 等写操作明确标 `risk="write"`；配套 `gate_harness_test/` 独立安全测试 harness
- **四层记忆系统**：会话记忆 / 用户画像 / 故障模式库 / 压缩归档，支持长对话
- **双轨工具调用**：Function Calling + MCP 协议，可扩展接入任意运维工具链
- **可复现评测闭环**：每轮跑评测自动采指纹（31 字段 hash）+ 阈值红线 + case 级 Trace 落盘；Langfuse 全链路追踪

## 架构

```
用户输入 → load_memory → Coordinator → [Worker ×N 并行] → Synthesizer → persist_memory → 输出
                              │                │                 │
                         生成故障假设      独立调工具验证     比对证据生成结论
                                                │
                                        policy 白名单硬拦 +
                                        search_sop 召回 / wiki_read 扩网
```

**技术栈**：
- 框架：LangGraph（状态图 + Send API 并行调度）
- 模型：DeepSeek / Qwen 系列（通过 OpenAI 兼容层统一调用）
- 检索：PostgreSQL + pgvector / BGE-M3 Embedding / BGE-Reranker / Wiki 图谱
- 安全：自研 Policy Gate（阶段白名单 + 审计日志）
- 前端：Chainlit（对话式交互 + 实时流式输出）
- 可观测：Langfuse（Trace / Span / Score 全链路）

## 快速开始

### 前置依赖
- Python 3.11+
- [uv](https://docs.astral.sh/uv/)（包管理器）
- Docker Desktop（PostgreSQL + pgvector）

### 安装与启动

```bash
# 安装依赖
make install

# 配置环境变量
make env
# 编辑 .env，填入 DEEPSEEK_API_KEY 或 DASHSCOPE_API_KEY

# 验证模型连通性
make test-llm

# 启动数据库
make db-up

# 启动 Chainlit 交互界面
make demo
```

### 运行评测

```bash
# 全量评测（56 条单轮，覆盖 10 类故障模式）
make eval

# 快速评测（10 case 烟测）
make eval-quick

# 全量 3 次取中位数（可靠 baseline）
make eval-median
```

### Wiki 图谱冒烟（可选）

```bash
# 生成 Wiki 覆盖率报告，验证 KB 内部链接图能建起来
uv run python scripts/wiki_report.py
```

### Policy Gate 安全测试（可选）

```bash
# L1 规则矩阵（秒级，可入 CI）
uv run python scripts/run_gate_tests.py --layer L1

# L3 强制调用（秒级）
uv run python scripts/run_gate_tests.py --layer L3

# L2 LLM 诱导（分钟级，需 LLM key）
uv run python scripts/run_gate_tests.py --layer L2

# 全跑
uv run python scripts/run_gate_tests.py --layer all
```

详见 [`docs/使用说明.md`](docs/使用说明.md)。

## 项目结构

```
opsagent/
├── core/
│   ├── graph/          # LangGraph 状态图（nodes / builder / state）
│   ├── llm/            # LLM 客户端（多模型路由 + 超时重试）
│   ├── memory/         # 四层记忆系统
│   ├── retrieval/      # RAG 检索链 + wiki_graph（Wiki 图谱，RAG 增强）
│   ├── policy.py       # 阶段化工具权限门（白名单 + 审计）
│   ├── tools/          # 工具注册与执行（FC + MCP）
│   └── prompts/        # Prompt 模板
├── app/                # FastAPI 服务（SSE 流式）
└── ui/                 # Chainlit 前端
eval/
├── dataset/cases/      # 评测用例（YAML，10 故障类型 × 难度分级）
├── metrics/            # 评分器（工具覆盖 + 引用命中 + 结论关键词）
├── fingerprint.py      # 评测指纹（31 字段 hash，保证可复现）
├── thresholds.py       # 阈值红线校验（CI 用）
├── trace.py            # case 级 trace 落盘（badcase 归因）
├── ab.py               # A/B 对比评测
├── runner.py           # 评测执行器（并发 + 重试 + 报告生成）
└── reports/            # 评测报告存档（运行产物）
gate_harness_test/      # Policy Gate 独立测试 harness（与业务 eval 完全解耦）
├── scenarios/          # D 直接调用 / R 角色冒充 / P SOP 投毒 / S 综合注入 / T 工具返回注入 / X cross_role
├── runners/            # L1 规则矩阵 / L2 LLM 诱导 / L3 强制调用
├── audit/              # 决策与危险执行审计日志
└── mock_data/          # 危险 mock 工具 / 毒化 SOP / role_switcher
docs/                   # 设计文档
tests/                  # 单测（含 test_wiki_graph / test_wiki_read）
```

## 评测体系

### 业务评测（`eval/`）

四层评测：L4 端到端任务正确率（核心）/ L3 检索质量 / L2 Ragas 双指标 / L1 单测。

**端到端评分公式**（`eval/metrics/`）：

```
score = 0.4 × tool_sequence_score + 0.3 × citation_score + 0.3 × conclusion_score − forbidden_penalty
```

**评分维度**：
- 工具覆盖率（Recall）：是否调用了正确的诊断工具
- 引用命中率（梯度）：是否引用了相关 SOP 文档（精确命中 1.0 / 同域 0.5 / 缺失 0）
- 结论关键词：最终诊断是否命中核心故障术语
- 禁忌词控制：是否输出了不应出现的误导性结论

**可复现性四件套**：
- `fingerprint.py` —— 每轮跑评测自动采集 31 字段指纹（git / 源码 hash / KB hash / 模型 / 检索参数），用 `ab.py` 对比两轮证明"分数差来自代码还是 vendor 抖动"
- `thresholds.py` —— 6 项硬阈值（total/cite/timeout/p95/tool_count/forbidden），破线 exit 1，可挂 CI
- `trace.py` —— 每 case 一个 JSONL（trace_id / wiki_jumps / 每步 tool_call 的 doc_ids+scores），badcase 归因不再靠离线探针
- `ab.py` —— A/B 对比工具：先 diff 指纹（控制变量证明），再 diff 分数

**当前指标**（v4，多轮中位）：

| 指标 | 数值 |
|------|------|
| 总分均值 | 0.83-0.87 |
| 引用命中率 | 0.82-0.86 |
| 工具 F1 | 0.97-1.00 |

### 安全测试（`gate_harness_test/`）

与业务评测完全解耦：`eval/` 测"答得对不对"，`gate_harness_test/` 测"被骗时能不能拦住"。三层 runner 覆盖：

| 层 | runner | 速度 | 测什么 |
|---|---|---|---|
| L1 | `matrix_runner` | 秒级 | 任意（阶段 × 危险工具）组合是否全 deny |
| L2 | `llm_induced_runner` | 分钟级 | 真用 LLM + 注入 prompt 诱导调危险工具，看 gate 拦不拦 |
| L3 | `forced_call_runner` | 秒级 | 绕过 LLM 直接构造 ToolCall 进 `_exec_tool`，验证执行层兜底 |

覆盖六类情景：**D** 直接调危险工具 / **R** 角色冒充 / **P** SOP 检索投毒 / **S** Synthesizer 阶段诱导 / **T** 工具返回藏指令 / **X** cross_role（双 prompt patch 删掉 LLM 自觉，纯验证 gate 兜底）。

## 设计文档

| 文档 | 说明 |
|------|------|
| [架构设计](docs/架构设计.md) | 项目定位、技术选型、系统链路、四层记忆、Wiki 图谱、Policy Gate、数据与评测 |
| [开发历程](docs/开发历程.md) | 单 Agent → Multi-Agent → Wiki 图谱 → Policy Gate 的关键演进节点与数据 |
| [使用说明](docs/使用说明.md) | 安装、启动、建索引、评测、安全测试的完整操作步骤 |

## 关于数据与命名

- **检索底座**为公开数据集，**评测数据为 LLM 合成**，故障形态提炼自真实生产经验。
- 服务命名：`edgectl-*`（合成假名）。

## License

MIT
