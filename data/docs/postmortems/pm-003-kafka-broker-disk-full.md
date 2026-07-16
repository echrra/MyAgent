# 故障复盘 PM-003：Kafka Broker 磁盘满导致事件丢失

## 基本信息

| 项目 | 内容 |
|---|---|
| 时间 | 2026-03-22 03:15 ~ 04:30（持续 75 分钟） |
| 影响服务 | backend-http（生产者）、algo（消费者） |
| 影响范围 | 设备状态变更事件延迟 + 部分事件丢失，审核任务堆积 |
| 触发告警 | P1：kafka_consumergroup_lag 凌晨 3:20 开始增长 |
| 发现人 | 监控告警 |

## 现象

凌晨 3:15，backend-http 日志开始出现 `kafka produce failed topic=device-event: broker not available`。producer 自动重试（attempt=1/2/3），约 30% 的消息发送失败后 producer pool exhausted。

同时 algo 的 `kafka_consumergroup_lag` 指标从凌晨 2:00 的 50 开始持续增长到 3:30 的 8000。

## 排查过程

| 时间 | 操作 | 发现 |
|---|---|---|
| 03:22 | 告警响应 | 确认 lag 不是消费慢导致（algo 消费速率正常） |
| 03:28 | 查 Kafka Broker | 3 台 Broker 中有一台磁盘使用率 98%（`df -h`），IO util 100% |
| 03:32 | 查磁盘满的原因 | 凌晨 2:00 开始的批量数据导入任务（另一个业务团队的操作），往该 Kafka 集群写入了大量临时数据 |
| 03:35 | 确认根因 | 磁盘满导致该 Broker 上的分区无法接受新写入 → leader 切换 + 部分分区 ISR 缩减 |

## 根因

**直接原因**：其他业务的批量数据导入任务写入了大量数据到共享 Kafka 集群，导致一台 Broker 磁盘满。

**根本原因**：
1. Kafka 集群没有按业务做 Topic 级别的磁盘配额（`retention.bytes`）
2. 批量任务的临时 topic 没有设置合理的 retention
3. 磁盘告警只设了单一阈值，缺少 75%/85%/90%/95% 这类分级预警，值班人员来不及处理

## 处置

1. **临时止血**（03:40）：清理批量任务的临时 topic + 调大 retention 清理速度，磁盘使用率 04:20 降至 70%。
2. **长期方案**：
   - 每个 topic 设置 `retention.bytes` 上限
   - 批量数据使用独立的 Kafka 集群（或至少独立 Broker）
   - 磁盘告警增加 75%/85%/90%/95% 分级通知，并结合增长速度判断紧急程度
   - producer 侧加"发送失败消息本地暂存 + 补发"机制

## 教训

1. **共享基础设施的"邻居效应"**——一个业务的批量任务可以拖垮整个 Kafka 集群。隔离是最有效的防护。
2. **producer 的 buffer 不是无限的**——`producer pool exhausted` 之后消息就丢了。业务侧必须有心跳/对账机制来发现"漏发消息"。

## 关联 SOP

- [SOP-F3: Kafka 故障排查](../sops/sop-f3-kafka-error.md)

## 关联故障模式

F3（Kafka 生产端失败）