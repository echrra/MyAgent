# SOP-F5: 数据库慢查询与超时排查

## 适用场景

- 日志出现：`query canceled: context canceled` / `long running query interrupted after <N>ms` / `driver: bad connection (ctx canceled)`
- 告警：`mysql_slow_query_total` 增长 / 连接池等待时长上升
- 用户反馈："列表页加载很久最后超时" / "统计报表打不开"

## 排查步骤

### 第 1 步：定位慢 SQL

从数据库 Prefix 的 ERRO 日志中提取 SQL 模板：

```
grep '"Prefix":"database"' /data/logs/backend-http/*.jsonl | grep '"Level":"ERRO"' | jq '{time, sql: .Content, latency: .sql_ms}'
```

### 第 2 步：分析慢的原因

拿到慢 SQL 后，判断属于哪种类型：

| 类型 | 特征 | 常见 SQL |
|---|---|---|
| **全表扫描** | rows 数量异常大，sql_ms 明显高于接口预算 | `SELECT * FROM t WHERE ...` 无索引 |
| **多表 JOIN** | 3+ 表 JOIN，rows 乘积爆炸 | 列表查询带关联 |
| **大表 COUNT** | `COUNT(*)` 无 WHERE 条件 | 统计/分页查询 |
| **锁等待** | `Waiting for table metadata lock` / `Lock wait timeout` | DDL + DML 并发 |
| **连接池满** | `Too many connections` | 慢查询堆积导致连接不释放 |

### 第 3 步：关联 F1 / F5 判断

关键问题：**是 SQL 自己慢，还是 SQL 被 ctx 取消导致报错？**

区别：
```
// SQL 自己慢（根因在 DB）
sql_ms=32000, rows:0, "long running query interrupted after 30000ms"
// 此时 DB 确实在执行 SQL，但超时被杀了

// SQL 被 ctx 取消（根因在上游）
sql_ms=150, rows:0, "query canceled: context canceled"
// DB 还没来得及执行完就被上游 cancel 了
```

### 第 4 步：处置

| 情况 | 处置 |
|---|---|
| SQL 自己慢 | 加索引 / 改写 SQL / 读写分离 |
| SQL 被上游取消 | 排查上游 ctx 为什么超时（→ SOP-F1）|
| 连接池满 | 增加 `max_connections` / 加连接池 / 限流 |
| 锁等待 | kill 阻塞的 DDL / 改在低峰期执行 |

## 常见根因

- **索引缺失/失效**：数据量增长后查询计划变差
- **分页深翻**：`LIMIT 100000, 20` 也会扫描前面所有行
- **ORM 自动生成低效 SQL**：如 N+1 查询、不必要的 JOIN
- **统计查询未走只读副本**：大量 COUNT/SUM 打到主库
- **长事务未提交**：持有锁阻塞其他查询

## 相关文档

- [SOP-F1: 级联超时排查](sop-f1-cascade-timeout.md)
- [SOP-F7: MySQL 业务错误排查](sop-f7-mysql-business-error.md)
- [中间件手册 - MySQL](../runbooks/mysql.md)