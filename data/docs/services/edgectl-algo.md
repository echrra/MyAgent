# edgectl-algo — 异步算法/审核消费服务

## 定位

算法/审核/异步任务消费端。从 Kafka 消费事件消息，执行审核、分析等耗时操作。是 F3（Kafka 生产端失败）的反面——消费端视角。

## 关键职责

- **Kafka 消费**：消费 `device-event` / `work-event` / `audit-task` 等多个 topic
- **审核任务**：执行内容/设备/工作流的异步审核
- **统计分析**：异步计算统计指标，写入 MySQL
- **回调通知**：处理完成后回调 backend-http 或发 Kafka 事件

## 上下游依赖

```
Kafka → algo → 审核模型服务 / 算法服务
            → object-storage（审核截图拉取）
            → MySQL（分析结果写入）
            → backend-http（结果回调）
```

## 常见故障模式

| 故障 | 关联 SOP |
|---|---|
| Kafka 消费滞后（lag 增长）| SOP-F3 |
| 对象存储拉取审核素材 ctx 取消 | SOP-F4 |
| 回调 backend-http 失败（HTTP 4xx/5xx）| SOP-F2 |
| 复杂分析 SQL 超时 | SOP-F5 |
| DNS 解析 backend-http 失败 | SOP-F6 |
| nil pointer panic | SOP-F9 |

## 关键指标

- `kafka_consumergroup_lag{topic="device-event"}`：消费滞后量
- `algo_audit_duration_ms_p99`：审核耗时 P99
- `algo_callback_failures_total`：回调失败量

## 日志特征

- Prefix：`algo` / `default`
- 常见 msg：`message consumed topic=<TOPIC>` / `audit completed` / `callback to backend failed`
- CallerPath 模式：`/service-x/internal/algo/*.go` / `/service-x/internal/kafka/consumer.go`

## 配置项

| 配置键 | 含义 | 默认值 | 调整建议 |
|---|---|---:|---|
| `algo.kafka.consumer_group` | 消费者组名 | `algo-consumer` | 同一业务消费组保持稳定，避免误建新组导致重复消费 |
| `algo.audit.timeout_ms` | 单次审核超时 | `30000` | 模型/审核服务慢时应降级或进入人工兜底 |
| `algo.callback.retry_count` | 回调重试次数 | `3` | 回调必须幂等，避免重复写结果 |
