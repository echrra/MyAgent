# Prometheus / 指标监控运维手册

## 适用服务

所有 edgectl-* 服务（各服务暴露 `/metrics` 端点，Prometheus 定期抓取）

## 核心指标命名规范

遵循 Prometheus 命名约定：`<namespace>_<metric>_<unit>`，标签用 `{}` 包裹。

## 告警阈值设计原则

这里保留生产常见默认阈值，便于落地；正式接入真实日志/指标时，再按服务基线、QPS、SLA 做校准。

1. **默认值用于起步**：先有告警，再根据误报/漏报调参。
2. **按接口分组**：普通接口、上传接口、部署接口、离线任务不要共用同一延迟阈值。
3. **多信号组合**：慢查询 + P99 上升 + 连接池高，比单独慢查询更可信。
4. **分级处理**：P3 观察、P2 告警、P1 严重、P0 可用性事故。

## 核心告警规则

### 服务健康

```yaml
# 服务存活
- alert: ServiceDown
  expr: up{job=~"edgectl-.*"} == 0
  for: 1m

# 错误率突增
- alert: HighErrorRate
  expr: rate(http_request_total{status=~"5.."}[5m]) > 0.05
  for: 2m
  note: "默认 5xx >5%；核心接口可从 >1% 开始预警"

# P99 延迟异常
- alert: HighLatency
  expr: histogram_quantile(0.99, rate(http_request_duration_seconds_bucket[5m])) > 30
  for: 3m
  note: "默认 P99 >30s 视为严重；普通接口建议另设 3s/5s 预警线"
```

### 数据库

```yaml
# 慢查询：需先明确 long_query_time，建议普通接口按 1s 统计
- alert: SlowQueries
  expr: rate(mysql_slow_query_total[5m]) > 0.1
  for: 5m
  note: "约等于 >6/min；P1 可用 >10/min 且接口 P99 上升"

# 连接池接近满
- alert: DBConnectionPoolHigh
  expr: mysql_threads_connected / mysql_max_connections > 0.8
  for: 2m
  note: "默认 >80%；>70% 预警，>85% 且请求排队时升级"
```

### Kafka

```yaml
# 消费滞后
- alert: KafkaLag
  expr: kafka_consumergroup_lag > 1000
  for: 5m
  note: "默认 lag >1000；更关键的是持续增长和预计追平时间"

# 分区无 Leader（严重）
- alert: KafkaOfflinePartition
  expr: kafka_broker_offline_partitions > 0
  for: 0m
  note: "离线分区通常不能容忍，按影响 Topic 判断 P1/P0"
```

### Redis

```yaml
# 命中率低
- alert: LowCacheHitRate
  expr: rate(redis_keyspace_hits_total[5m]) / (rate(redis_keyspace_hits_total[5m]) + rate(redis_keyspace_misses_total[5m])) < 0.8
  for: 5m
  note: "默认 <80%；如果 DB QPS/慢查询同步上升，说明影响已传导"

# 内存使用高
- alert: RedisMemoryHigh
  expr: redis_used_memory / redis_max_memory > 0.8
  for: 3m
  note: "默认 >80%；>90% 或出现关键 key 淘汰时升级"
```

### 应用层

```yaml
# Panic 发生（F9）：500 只是代理信号，必须配合日志关键词确认
- alert: PanicDetected
  expr: rate(http_request_total{status="500"}[5m]) > 0.01
  for: 1m
  note: "需要同时检索 panic / recovered from panic 关键词"

# 关键词告警（F10 Token decode 陷阱）
# 注意：这里不能用 status_code / Level 过滤
- alert: TokenDecodeError
  expr: rate(log_line_total{msg=~".*decode error.*"}[5m]) > 10
  for: 1m
  note: "默认 >10/s；真实接入后按正常误报率调整"

# F2 陷阱：HTTP 200 + 业务 errCode!=0
# 纯 Prometheus 只能看到 status=200，看不到 response body 里的 errCode。
# 这条需要日志解析 / APM / Agent 跨数据源关联：Prometheus 发现 200 正常，日志里却有 errCode 非 0。
- alert: BusinessErrorWrapped
  expr: rate(log_line_total{msg=~".*errCode=[1-9].*|.*operation succeeded but action failed.*"}[5m]) > 5
  for: 2m
  note: "用于捕获 HTTP 200 包裹业务失败；如果只有 Prometheus 指标，需要 Agent 结合日志确认"
```

## 常用查询（PromQL）

```promql
# 各服务 QPS
sum(rate(http_request_total[1m])) by (service)

# 各服务 P99 延迟
histogram_quantile(0.99, sum(rate(http_request_duration_seconds_bucket[5m])) by (service, le))

# 某个接口的错误率
sum(rate(http_request_total{path="/api/v1/deploy"}[5m])) by (status)
```

## 告警分级

| 级别 | 定义 | 通知方式 | 响应时间 |
|---|---|---|---|
| P0 | 全链路不可用 / 核心功能大面积不可用 | 电话 + 群通知 | 5 分钟内 |
| P1 | 核心功能部分不可用 / 错误率明显高于基线 | 群通知 | 15 分钟内 |
| P2 | 指标异常但影响范围可控 | 群通知 | 30 分钟内 |
| P3 | 预警 / 趋势类 | 日报汇总 | 次日处理 |

## Grafana Dashboard 推荐面板

1. **概览行**：各服务 QPS + P99 + Error Rate（3 列）
2. **数据库行**：慢查询数 + 连接池使用率 + 主从延迟
3. **Kafka 行**：各 Consumer Group Lag 时序图
4. **Redis 行**：命中率 + 内存使用率
5. **应用层行**：Panic 次数 + Token Decode Error 次数（F10 专用面板）

## 故障关联

- F1 级联超时 → P99 延迟曲线多服务同时突增
- F2 HTTP 错误 → 5xx Error Rate 或 `200+errCode` 突增
- F3 Kafka 滞后 → Consumer Group Lag 持续增长
- F5 SQL 超时 → DB 慢查询曲线 + 接口延迟同时上升
- F10 Token 问题 → **默认告警看不到，必须有关键词维度面板**
