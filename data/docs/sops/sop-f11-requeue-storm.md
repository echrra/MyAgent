# SOP-F11: 消息队列 REQUEUE_STORM 排查（任务反复重投）

## 适用场景

- 日志出现：`[REQUEUE_STORM] task requeued too many times: taskId=<ULID> topic=... requeueCount=<N> threshold=<N>`
- 用户反馈："某个任务一直显示处理中，很久也不出结果" / "任务状态卡在中间态"
- 现象：单条消息在 watcher 中被反复消费但每次都失败，requeueCount 持续攀升；消息队列积压逐渐扩大

## 特征识别

REQUEUE_STORM 的核心是"消费失败但不 ACK"的循环。区分它与其他消息问题：

| 症状 | REQUEUE_STORM | Kafka 生产失败（F3） | 消费者卡死 |
|---|---|---|---|
| 消息是否进入 topic | 已进入 | 未进入 | 已进入 |
| 消费者是否在跑 | 在跑 | 无关 | 不在跑 |
| requeueCount | **持续增大** | 无 | 恒定 |
| 责任方 | 业务处理逻辑 | broker/producer | consumer 进程 |

## 排查步骤

### 第 1 步：确认是否单 task 循环还是全局风暴

```
统计条件：service=edgectl-backend-watcher, keyword=REQUEUE_STORM, time_window=now-30min~now
关注字段：taskId, requeueCount
判定：
- 少数 taskId 上的 requeueCount 持续升 → 局部循环（单任务毒药消息）
- 多个 taskId 都在攀升 → 全局风暴（业务处理逻辑整体故障）
```

### 第 2 步：找到"单条消息为什么处理失败"

拿到一个具体的 taskId（例如 requeueCount 最高的那个），查它的失败原因：

```
关键词：taskId 值，service=edgectl-backend-watcher
查找模式：handleTaskFailure / raw error / processing message failed
```

失败原因通常有三类：
- **业务校验失败**（如 errCode: motion_tilt_danger）—— 这类**不该重试**，重试永远不会成功
- **下游服务临时不可用**（HTTP 504 / DNS 失败）—— 应该重试，但要有次数上限
- **消息体损坏 / 反序列化失败** —— 应该直接进死信

### 第 3 步：判断处置方向

| 失败原因 | 是否应该 requeue | 处置 |
|---|---|---|
| 业务校验失败 | ❌ **不应重试** | 立即改代码或配置：把该 errCode 加入"永久失败"列表，转死信 |
| 下游临时不可用 | ✅ 可重试但要有上限 | 检查 requeue policy 是否有 max_retries；补上就能自愈 |
| 消息体损坏 | ❌ | 直接死信 + 告警 |
| 消费者代码 panic | ❌ | 修复代码 bug |

### 第 4 步：立即止血

- **手动跳过毒药消息**：把 requeueCount 超过阈值的 taskId 手工标记为 failed / 转死信队列
- **回滚 requeue policy**：如果最近改过重试次数配置，回滚
- **限流消费者**：如果全局风暴导致下游雪崩，先降 watcher 并发度避免放大

### 第 5 步：根治

- 给 watcher 增加"永久失败错误码白名单"，命中即转死信不 requeue
- 每类业务失败码明确 retry / no-retry 语义（业务方 review）
- 加告警：`requeueCount > threshold` 或 "同 taskId requeue 次数 > 5" 立即告警，避免风暴放大到不可控

## 常见根因总结

| 场景 | 一句话 | 责任方 |
|---|---|---|
| 业务校验失败被无脑重试 | 该失败该丢却在死循环 | 业务处理器（未区分 retriable/non-retriable） |
| 下游服务临时抖动 | 重试策略缺上限 | requeue policy 配置 |
| 消息体损坏 | 反序列化失败仍 requeue | 消费者错误处理 |
| 消费者代码 panic 后重启 | 消息被 kafka 认为未消费又派回 | 消费者健壮性 |

## 相关文档

- [SOP-F3: Kafka 生产端失败](sop-f3-kafka-error.md)
- [SOP-F7: MySQL 业务错误](sop-f7-mysql-business-error.md)（业务错误码分类思路）
- [PM-011: motion-training REQUEUE_STORM 事件](../postmortems/pm-011-requeue-storm.md)
