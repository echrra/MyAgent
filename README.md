# OpsAgent

智能运维诊断 Agent —— 基于假设驱动的多 Agent 并行故障定位系统。

## 简介

OpsAgent 是一个面向微服务架构的智能运维诊断工具，通过 Multi-Agent 协作实现自动化故障根因分析。输入一段故障描述或告警信息，OpsAgent 会自动生成故障假设、并行调度工具验证、综合证据给出诊断结论。

**核心能力**：
- 假设驱动并行诊断：Coordinator 生成多故障假设，Worker 并行验证，Synthesizer 综合证据
- 混合检索 RAG：pgvector 向量检索 + BM25 全文检索 + RRF 融合 + Rerank 精排
- 四层记忆系统：会话记忆 / 用户画像 / 故障模式库 / 压缩归档，支持长对话
- 双轨工具调用：Function Calling + MCP 协议，可扩展接入任意运维工具链
- 可观测评测闭环：Langfuse 全链路追踪 + 自建评测体系驱动迭代

## 架构

```
用户输入 → load_memory → Coordinator → [Worker ×N 并行] → Synthesizer → persist_memory → 输出
                              │                │                 │
                         生成故障假设      独立调工具验证     比对证据生成结论
```

**技术栈**：
- 框架：LangGraph（状态图 + Send API 并行调度）
- 模型：DeepSeek / Qwen 系列（通过 OpenAI 兼容层统一调用）
- 检索：PostgreSQL + pgvector / BGE-M3 Embedding / BGE-Reranker
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
```

## 项目结构

```
opsagent/
├── core/
│   ├── graph/          # LangGraph 状态图（nodes / builder / state）
│   ├── llm/            # LLM 客户端（多模型路由 + 超时重试）
│   ├── memory/         # 四层记忆系统
│   ├── retrieval/      # RAG 检索链（embedding / rerank / query_rewrite）
│   ├── tools/          # 工具注册与执行（FC + MCP）
│   └── prompts/        # Prompt 模板
├── app/                # FastAPI 服务（SSE 流式）
└── ui/                 # Chainlit 前端
eval/
├── dataset/cases/      # 评测用例（YAML，10 故障类型 × 难度分级）
├── metrics/            # 评分器（工具覆盖 + 引用命中 + 结论关键词）
├── runner.py           # 评测执行器（并发 + 重试 + 报告生成）
└── reports/            # 评测报告存档（运行产物）
docs/                   # 设计文档
tests/                  # 单测
```

## 评测体系

自建端到端评测，覆盖 10 类微服务故障模式（级联超时、Kafka 异常、DNS 解析失败、SQL 上下文取消等），按 easy / medium / hard 分级。

**评分维度**：
- 工具覆盖率（Recall）：是否调用了正确的诊断工具
- 引用命中率（梯度）：是否引用了相关 SOP 文档（精确命中 1.0 / 同域 0.5 / 缺失 0）
- 结论关键词：最终诊断是否命中核心故障术语
- 禁忌词控制：是否输出了不应出现的误导性结论

**当前指标**（v4，3×55 全量验证）：

| 指标 | 数值 |
|------|------|
| 总分均值 | 0.83-0.87 |
| 引用命中率 | 0.82-0.86 |
| 工具 F1 | 0.97-1.00 |

## 设计文档

| 文档 | 说明 |
|------|------|
| [架构设计](docs/架构设计.md) | 项目定位、技术选型、系统链路、四层记忆、数据与评测 |
| [开发历程](docs/开发历程.md) | 单 Agent → Multi-Agent 的关键演进节点与数据 |
| [使用说明](docs/使用说明.md) | 安装、启动、建索引、评测的完整操作步骤 |

## 关于数据与命名

- **检索底座**为公开数据集，**评测数据为 LLM 合成**，故障形态提炼自真实生产经验。
- 服务命名：`edgectl-*`（合成假名） 


## License

MIT
