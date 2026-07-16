# Kafka 运维手册

## 适用服务

- **生产端**：edgectl-backend-http（发布设备事件/工作流事件）、edgectl-backend-watcher（发布状态变更事件）
- **消费端**：edgectl-algo（审核任务消费）、edgectl-ugc（内容审核消费）、edgectl-notification（通知消息消费）

## 关键 Topics

| Topic | 生产者 | 消费者 | 消息量级 |
|---|---|---|---|
| `device-event` | backend-http | algo, watcher | 高（每台设备状态变更一条）|
| `work-event` | backend-http, scheduler | algo | 中（每次部署/指令下发一条）|
| `audit-task` | backend-http | ugc | 低（仅审核类操作） |
| `notification-event` | backend-http, admin | notification | 低（仅告警/通知触发）|
| `status-change` | watcher | algo | 中（对账时可能批量） |

## 关键指标

| 指标 | 含义 | 默认告警阈值 | 补充判断 |
|---|---|---|---|
| `kafka_consumergroup_lag` | 消费滞后量 | `> 1000` 或持续增长 | 不只看绝对值，重点看是否持续增长；预计追平时间 >10m 时升级 |
| `kafka_producer_record_error_rate` | 生产失败率 | `> 0.1%` | 关键 Topic 发送失败要优先处理；>1% 通常已影响业务 |
| `kafka_broker_under_replicated_partitions` | 副本不足的分区 | `> 0` | 短暂 leader 切换可观察，持续不恢复需处理 |
| `kafka_broker_offline_partitions` | 离线分区 | `> 0` | 通常直接 P1/P0，按影响 Topic 判断 |
| `kafka_broker_disk_used_pct` | Broker 磁盘使用率 | `> 80%` | 建议 75%/85%/90%/95% 分级预警，结合增长速度判断紧急程度 |

## 常用排查命令

```bash
# 查看消费者组 lag
kafka-consumer-groups --bootstrap-server <BROKER> \
  --group algo-consumer --describe

# 查看 topic 详情
kafka-topics --bootstrap-server <BROKER> \
  --topic device-event --describe

# 查看最新消息（调试用，勿在生产频繁执行）
kafka-console-consumer --bootstrap-server <BROKER> \
  --topic device-event --max-messages 5 --from-beginning
```

## 生产者常见问题和处理

| 问题 | 现象 | 处理 |
|---|---|---|
| broker 不可达 | `broker not available` | 检查 Kafka 集群 + 网络 |
| 消息过大 | `message too large` | 减小消息体 / 改 `max.message.bytes` |
| 生产者池耗尽 | `producer pool exhausted` | 扩容生产者 / 限流上游 |
| leader 切换 | `leader for partition not available` | 等待恢复（秒级），观察重试 |
| 发送超时 | `produce sync timeout` | 检查 broker 负载 / 网络延迟 |

## 消费端常见问题和处理

| 问题 | 现象 | 处理 |
|---|---|---|
| 消费滞后 | lag 持续 `> 1000` 或持续增长 | 增加消费者实例 / 增加分区 / 优化处理逻辑 |
| 频繁 rebalance | 消费者日志中 `rebalance` 频繁 | 调大 `max.poll.interval.ms` / 减少单条处理时间 |
| 消息重复消费 | 同一条消息被处理多次 | 业务侧加幂等（以 message.key 去重）|

## 消费者配置参考

```yaml
# 消费者关键参数
consumer:
  group_id: algo-consumer
  max_poll_records: 500           # 单次拉取最大条数，处理慢时要降低
  max_poll_interval_ms: 300000    # 两次 poll 最大间隔（超过触发 rebalance）
  session_timeout_ms: 30000       # 会话超时
  enable_auto_commit: false       # 手动提交（保证至少处理一次）
```

## 故障关联 SOP

- [SOP-F3: Kafka 故障排查](../sops/sop-f3-kafka-error.md)
