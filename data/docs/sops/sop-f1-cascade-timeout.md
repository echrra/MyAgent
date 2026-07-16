# SOP-F1: 级联超时排查指南

## 适用场景

- 告警：`http_request_duration_seconds_p99` 突增，多个服务同时告警
- 用户反馈："操作提交后一直转圈，最后提示失败"
- 日志特征：`context deadline exceeded` / `context canceled` / `timeout` 在多个服务日志中连锁出现

## 排查步骤

### 第 1 步：确认故障范围

查 gateway 最近 5 分钟的 status_code 分布：

```
统计条件：service=edgectl-gateway, time_window=now-5min~now
关注字段：status_code, path, latency_ms
异常信号：504/502 比例突增，或 P99 latency 明显高于该接口历史基线；若超过网关超时阈值（如 30s）通常已是严重故障
```

### 第 2 步：找到"最先慢"的服务

从 gateway 的慢请求 TraceId 入手，沿调用链向下找：

```
查 Trace：按 TraceId 查全链路 span，按 duration_ms 降序排列
关键判断：哪个服务的 span 时间最长？是它自己慢还是它的下游慢？
```

**关键技巧**：看 Trace 的时间轴——如果 A 调 B，B 的 span 开始时间早于 A 的 timeout 点，且 B 的 duration 异常长，那 B 是"首先慢"的服务。

### 第 3 步：分析"首先慢"服务的根因

定位到具体服务后，按下面三类分支继续查，先用最直接的信号缩小范围：

- **如果是 DB 慢**：`SHOW FULL PROCESSLIST` + 查 `mysql_slow_query_total` / 慢 SQL 日志 → 跳转 SOP-F5；如果出现 1062/1364/1064 等业务错误 → 跳转 SOP-F7
- **如果是调下游 HTTP 慢**：查该服务日志中 `downstream.*returned status` / `upstream response errCode`，同时确认是否存在 `HTTP 200 + errCode!=0` → 跳转 SOP-F2
- **如果是自身 OOM/GC/Panic**：查 `go_memstats_heap_alloc_bytes` / `go_goroutines` / `go_gc_duration_seconds`；若有 `panic` / `nil pointer` → 跳转 SOP-F9

### 第 4 步：止损优先

| 严重等级 | 处置 |
|---|---|
| P0（影响全部用户） | 扩容"首先慢"的服务 + 上游开启熔断 |
| P1（部分用户/接口慢） | 限流上游 + 排查慢接口 |
| P2（偶发超时） | 观察 + 排查慢链路 |

## 常见根因

- **下游服务负载过高**：请求排队导致 RT 上升，传导上游
- **数据库慢查询**：某条 SQL 执行计划变差，拖慢整个请求线程池
- **网络抖动**：服务间 gRPC/HTTP 连接超时
- **线程池/连接池耗尽**：上游等待下游的连接被占满，新请求直接排队

## 为什么叫"级联"

因为微服务调用链是 A→B→C。C 慢了 → B 等 C 超时 → A 等 B 超时。一条链上所有服务都出现 timeout 日志，但根因只在最下游。

## 相关文档

- [edgectl-backend-http 服务说明](../services/edgectl-backend-http.md)
- [edgectl-backend-scheduler 服务说明](../services/edgectl-backend-scheduler.md)
- [SOP-F2: 下游 HTTP 错误排查](sop-f2-downstream-http-error.md)
- [SOP-F5: SQL 超时排查](sop-f5-sql-ctx-canceled.md)