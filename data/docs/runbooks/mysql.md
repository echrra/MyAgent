# MySQL 运维手册

## 适用服务

edgectl-backend-http、edgectl-admin、edgectl-backend-watcher、edgectl-algo（所有需要持久化的服务）

## 常用排查命令

```sql
-- 查看当前正在执行的查询
SHOW FULL PROCESSLIST;

-- 查看慢查询（慢查询日志已开启时）
-- slow_query_log_file 位置见 my.cnf

-- 查看当前连接数
SHOW STATUS LIKE 'Threads_connected';

-- 查看锁等待
SELECT * FROM information_schema.INNODB_LOCK_WAITS;

-- 查看最近死锁
SHOW ENGINE INNODB STATUS\G
```

## 慢查询口径

默认生产口径：

- `long_query_time = 1s`：普通后台/管理类接口的慢查询统计口径
- 核心高频接口可额外观测 `500ms` 以上查询
- 离线/报表类 SQL 单独分组，不和在线接口共用阈值

## 关键指标

| 指标 | 含义 | 默认告警阈值 | 补充判断 |
|---|---|---|---|
| `mysql_slow_query_total` | 慢查询数 | `> 10/min` | P3：>1/min 持续 10m；P2：>5/min 持续 5m；P1：>10/min 且接口 P95/P99 上升 |
| `mysql_threads_connected` | 当前连接数 | `> 80% of max_connections` | >70% 预警，>85% 且请求排队需升级 |
| `mysql_innodb_row_lock_waits` | 行锁等待次数 | `> 5/sec` | 重点看是否伴随 `Lock wait timeout` / 写入失败 |
| `mysql_qps` | 每秒查询数 | 突增 `3×` 需关注 | 结合业务流量判断，避免把正常活动流量当故障 |
| `mysql_replication_lag_seconds` | 主从延迟 | `> 5s` | >30s 或影响读一致性时升级 |

## 常见错误码速查

| 错误码 | 含义 | 处理思路 |
|---|---|---|
| 1062 | Duplicate entry | 检查幂等逻辑 / 加 INSERT IGNORE |
| 1364 | Field has no default | 补齐代码或加 DEFAULT 值 |
| 1064 | SQL syntax error | 查拼 SQL 代码 |
| 1213 | Deadlock | 优化事务顺序 / 加重试 |
| 2003 | Can't connect | 检查 MySQL 服务存活 + 网络 |
| 1040 | Too many connections | 检查慢查询堆积 → 临时调大连接数 |
| 1205 | Lock wait timeout | 检查是否有长事务未提交 |

## 日常维护

### 索引维护

- 每周检查 `pt-duplicate-key-checker`（重复索引）
- 每月检查未使用索引（`sys.schema_unused_indexes`）
- 大表 DDL 用 `pt-online-schema-change`（避免锁表）

### 慢查询跟进

- 每日查看慢查询 Top 10
- 慢 SQL 需要记录：SQL 模板、调用接口、执行耗时、扫描行数、执行计划
- 紧急慢 SQL：先 `EXPLAIN` 分析 → 加索引 / 改写 SQL → 观察 P95/P99 是否恢复

### 连接池配置（应用侧）

```yaml
# GoFrame 数据库配置参考
# 注意：所有服务 max_open 总和不要压满 MySQL max_connections
database:
  max_idle: 10          # 最大空闲连接
  max_open: 100         # 最大打开连接
  max_lifetime_ms: 1800000  # 连接最大存活时间（30min）
```

## 故障关联 SOP

- [SOP-F5: 慢查询/超时](../sops/sop-f5-sql-ctx-canceled.md)
- [SOP-F7: 业务错误 1062/1364/1064](../sops/sop-f7-mysql-business-error.md)
