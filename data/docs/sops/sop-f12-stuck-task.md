# SOP-F12: 任务状态卡住排查（STUCK_TASK / dropped task）

## 适用场景

- 日志出现：`[STUCK_TASK] suspected dropped task: id=<ULID> type=<TYPE> status=<STATUS> ageSeconds=<N>`
- 用户反馈："任务提交半小时了还是显示处理中" / "看不到结果、也没报失败"
- 现象：任务状态停留在中间态（如 `PreviewReady` / `Processing`），既没往下推进也没被主动 fail 掉

## 特征识别

STUCK_TASK 是**状态机没有转移**的问题，与"任务失败"不同：

| 症状 | STUCK_TASK | 任务失败（有明确 error） |
|---|---|---|
| 状态字段 | 中间态（Processing/Ready） | 终态（Failed/Success） |
| 有无错误日志 | 通常无 | 有 |
| 有无告警 | 只有 `[STUCK_TASK]` 巡检打的 | 业务失败链路的 ERRO |
| 用户体验 | 看不到反馈，最难受 | 至少能重试或收到失败通知 |

STUCK_TASK 通常由巡检器（stuck_task_reconciler）主动扫出，日志前缀是 `[STUCK_TASK]`。

## 排查步骤

### 第 1 步：找到卡住的 task 的完整生命周期

以 stuck task 的 id 为关键词，查所有相关日志（不限 Level）：

```
关键词：taskId 值
service：不指定（跨服务串联）
time_window：从 stuck 检测点向前推 ageSeconds + 缓冲
```

按时间排序看：
- 最后一条 INFO/DEBU 日志停在哪一步？
- 是消息发出去没收到 ACK，还是收到了 response 但状态没落库？
- 是否有 panic / exit / restart 打断处理？

### 第 2 步：定位"卡在哪一步"

STUCK_TASK 的常见卡点：

| 卡点 | 症状 | 排查 |
|---|---|---|
| 消息投出未消费 | 生产者日志有，消费者无 | 查 topic 消费组 lag，是不是消费者挂了 |
| 消费到但处理中断 | 消费者收到，处理到一半没 log | 查是否有 panic / OOMKilled / pod restart |
| 处理完了状态没写回 | 有下游成功日志，但 DB 状态未更新 | 查数据库更新失败（f7）或事务回滚 |
| 依赖回调未到达 | 等回调超时 | 查回调侧是否发出、网关是否路由丢失 |

### 第 3 步：判断是"个案卡死"还是"系统性堵塞"

- 单条 task 卡：局部问题，通常是 pod restart 或消息丢 —— 直接干预单条即可
- 一批 task 同时卡：系统性问题（消费者停了、DB 死锁、下游服务全挂），要马上告警扩大排查

### 第 4 步：立即止血

- **手动推进状态**：如果能确认下游其实处理成功了，管理员手工把状态推到 Success
- **主动重试**：如果确认卡在消息未消费，重新投递
- **扩容消费者**：如果是消费能力不够导致积压变卡，先扩 pod 数

### 第 5 步：根治

- 每种任务类型都要有"最长处理时长"上限，超时自动 fail 而不是无限等
- stuck_task_reconciler 应该主动给用户展示，而不是只打 ERRO 日志给运维（用户看不到"我卡住了"是最糟体验）
- 状态机的每个中间态都要有兜底 timeout

## 常见根因总结

| 场景 | 一句话 | 责任方 |
|---|---|---|
| pod OOMKilled 后消息丢失 | 消费者被杀在处理中间 | 内存配置 / 消息 ack 时机 |
| 状态更新事务回滚 | DB 层失败但外部看不到 | 事务边界与错误处理 |
| 依赖回调没有 timeout | 死等外部通知 | 状态机设计 |
| 消息路由错了消费不到 | 生产/消费 topic 名不匹配 | 配置管理 |

## 相关文档

- [SOP-F11: REQUEUE_STORM](sop-f11-requeue-storm.md)（都是消息处理链路问题）
- [SOP-F5: SQL ctx canceled](sop-f5-sql-ctx-canceled.md)（数据库事务失败可能导致状态未落库）
- [PM-012: MotionTraining 任务大规模卡在 PreviewReady](../postmortems/pm-012-stuck-task-preview-ready.md)
