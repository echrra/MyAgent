# 业务样本抽象产物（数据层 2）

> **三层架构里的中间层**：检索语料用 [data/logs/real/](../real/)（LogHub 公开数据集），业务语义模板用本目录。
> 详见 [docs/架构设计.md](../../../docs/架构设计.md)（第五节：数据策略与评测）。

## 这里存什么

| 文件 | 入库 | 说明 |
|---|---|---|
| `schema.<service>.yaml` | ✅ | 字段定义、类型、等级分布、服务命名约定 |
| `fault_phrases.<service>.yaml` | ✅ | 故障短语清单 + 共现约束（级别 / 路径 / 状态码 / CallerPath / Stack 模板） |
| `README.md` | ✅ | 本文件 |

## 为什么只入抽象产物，不入原始/样本日志

1. **LLM Generator 真正需要的是结构 + 短语，不是大段样本**：字段 schema 给约束，故障短语给关键词，比一堆冗长日志更有效
2. **入库内容可解释、可审计**：yaml 是声明式的，读一眼就知道"暴露了什么"
3. **原始样本天然存在边角风险**（base64 内部串、自定义 ID 体系、设备代号），不进仓库最稳

所以：**抽象产物入库（yaml）、原始观察留在脑子里 / 个人 Obsidian / 公司内部文档**。

## 怎么加新服务

1. 在工作站本地（**绝不入仓**）收集 30-50 条该服务的真实日志行（任意来源：日志服务 / 日志文件 / kubectl logs）
2. **人工通读**：识别字段约定、级别分布、典型故障短语（绝不复制原始数据到代码或 yaml）
3. 把抽象结论写到本目录：
   - 字段约定 → `schema.<service>.yaml`
   - 故障短语（占位符化）→ `fault_phrases.<service>.yaml`，所有业务串用 `<SN>` / `<ULID>` / `<HOST>` / `<DB>` 等占位符
4. （可选）把本地原始样本归档到本机 Obsidian / 个人文档目录，**仓库外**

## 已收录服务

- **edgectl** —— 边缘设备管控示例服务（虚构代号，GoFrame 微服务 + LogHub 采集）
  - [schema.edgectl.yaml](schema.edgectl.yaml) —— 容器元数据 / glog 应用层 / 5 类业务字段 / 等级分布 / 服务清单
  - [fault_phrases.edgectl.yaml](fault_phrases.edgectl.yaml) —— 10 类故障 + 正常基线
