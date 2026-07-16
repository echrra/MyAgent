# SOP-F3: Kafka 消息队列故障排查

## 适用场景

- 告警：`kafka_consumergroup_lag` 持续增长
- 日志出现：`kafka produce failed topic=<TOPIC>` / `broker not available` / `retry sending message attempt=<N>`
- 用户反馈："操作提交后没有反应" / "审核结果迟迟不出"

## 排查步骤

### 第 1 步：确认故障方向

先区分是**生产端失败**还是**消费端滞后**：

| 信号 | 方向 | 影响 |
|---|---|---|
| produce failed / broker not available | 生产端 | 消息没发出去——业务操作丢失 |
| consumergroup_lag 增长 | 消费端 | 消息积压——异步任务延迟 |
| 两者同时出现 | 可能是 Broker 本身问题 | 全链路瘫痪 |

### 第 2 步：生产端排查

查生产者日志中的失败原因：

```
grep "kafka produce failed" /data/logs/*.jsonl | jq '{time, msg, topic}'
```

常见失败原因：
- `broker not available` → 检查 Kafka Broker 是否存活、网络是否通
- `message too large` → 消息体超过 `max.message.bytes`，需要拆分或压缩
- `producer pool exhausted` → 生产者池满了，需要扩容或加限流
- `leader for partition not available` → 分区 Leader 切换中，等待恢复

生产端的重试是自动的——看到 `retry sending message attempt=1/2/3` 是正常现象，最终 attempt 失败才需要关注。

### 第 3 步：消费端排查

查 `kafka_consumergroup_lag` 指标：

```
指标：kafka_consumergroup_lag
标签：topic, consumer_group
查看：lag 的增长速度——快速增长说明消费跟不上生产
```

消费慢的常见原因：
- 消费者实例数 < 分区数（加实例）
- 消费处理逻辑变慢（看 algo/ugc 的 audit_duration_ms）
- 消费者重启 / rebalance 频繁

### 第 4 步：处置

| 情况 | 处置 |
|---|---|
| Broker 挂了 | 恢复 Kafka 集群（通常是基础设施团队职责）|
| 生产端失败（瞬时）| 重试 + 观察，检查网络 |
| 消费滞后（持续） | 扩消费者实例 / 增加分区 / 优化处理逻辑 |
| 消息体过大 | 改为引用传递（消息里只传 ID，消费者自己查） |

## 常见根因

- **Kafka Broker 磁盘满 / IO 高**：导致生产写入慢/失败
- **消费者处理慢**：下游 DB 慢拖慢每条消息处理
- **Rebalance 风暴**：消费者频繁加入/退出，导致分区反复重分配
- **网络分区**：消费者和 Broker 之间网络不通

## 相关文档

- [edgectl-algo 服务说明](../services/edgectl-algo.md)
- [edgectl-notification 服务说明](../services/edgectl-notification.md)
- [中间件手册 - Kafka](../runbooks/kafka.md)