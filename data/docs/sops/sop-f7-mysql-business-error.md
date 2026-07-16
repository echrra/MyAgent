# SOP-F7: MySQL 业务错误排查（1062/1364/1064）

## 适用场景

- 日志出现：`Error 1062: Duplicate entry` / `Error 1364: Field doesn't have a default value` / `Error 1064: SQL syntax error`
- 用户反馈："提交报错" / "数据保存失败" / "导入功能报错"
- 现象：特定操作失败率高，但数据库服务本身指标正常（不是 DB 挂了）

## 排查步骤

### 第 1 步：识别错误码

从 ERRO 日志中提取 MySQL 错误码：

```
grep '"Prefix":"database"' /data/logs/*.jsonl | grep '"Level":"ERRO"' | jq '{time, sql: .msg, error: .Content}'
```

### 第 2 步：按错误码分类处理

#### 1062 — Duplicate entry（唯一键冲突）

**现象**：`Duplicate entry '<VALUE>' for key '<TABLE>.<INDEX>'`

**根因**：相同的唯一键值被重复插入。常见于：
- 并发请求同时插入同一条数据
- 重试逻辑没有处理好幂等——第一次成功了，第二次重试又插了一次
- 用户重复点击提交按钮

**排查方向**：
1. 找冲突的 key 和 value
2. 查 TraceId——看是并发还是重试导致
3. 如果是并发：加分布式锁 / 用 `INSERT IGNORE` / `ON DUPLICATE KEY UPDATE`
4. 如果是重试：修正重试逻辑，先查是否存在再决定插入

#### 1364 — Field doesn't have a default value

**现象**：`Field '<FIELD>' doesn't have a default value`

**根因**：INSERT 语句缺少某个必填字段。常见于：
- 表结构新增了 NOT NULL 字段，但代码没同步更新
- 某条业务数据中该字段确实为空

**排查方向**：
1. 对比代码中的 INSERT 语句和数据库表结构——找到缺失的字段
2. 检查是"所有 INSERT 都缺"还是"某些场景缺"（后者更难找）
3. 临时止血：给字段加 DEFAULT 值
4. 根治：更新代码补齐字段

#### 1064 — SQL syntax error

**现象**：`You have an error in your SQL syntax near '<SNIPPET>'`

**根因**：拼接 SQL 时字符串处理出错。常见于：
- 动态拼接 ORDER BY / GROUP BY 时字段名有特殊字符
- 动态拼接 WHERE 条件时字符串没加引号
- 拼 SQL 时有未替换的占位符

**排查方向**：
1. 这是代码 bug，不是运维问题——需要看具体 SQL 和参数
2. 检查最近是否有代码变更（发布/配置变更）
3. 如果是 ORM 自动生成的 SQL，检查 ORM 版本是否兼容

### 第 3 步：处置

| 错误码 | 紧急程度 | 处置 |
|---|---|---|
| 1062 | P1 | 先止血（查重再插入）→ 根治（加幂等） |
| 1364 | P1 | 先止血（加 DEFAULT）→ 根治（改代码） |
| 1064 | P0 | **必须代码修复**，无法通过配置绕开 |

## 常见根因总结

| 错误 | 一句话 | 责任方 |
|---|---|---|
| 1062 | 有人重复插了一样的数据 | 业务逻辑（幂等缺失） |
| 1364 | 代码少写了一个字段 | DDL 和代码脱节 |
| 1064 | 拼 SQL 写错了 | 代码 bug |

## 相关文档

- [SOP-F5: SQL 超时排查](sop-f5-sql-ctx-canceled.md)
- [中间件手册 - MySQL](../runbooks/mysql.md)