# edgectl-ugc — 用户内容审核服务

## 定位

负责用户内容、工单附件、设备截图等 UGC 内容的审核。包含机审、人审、审核结果回调等链路。

## 关键职责

- **审核任务接收**：从 Kafka 消费 `audit-task`
- **机审调用**：调用审核模型服务 / 第三方审核接口
- **人审队列**：机审不确定时进入人工审核
- **结果回写**：审核完成后写 MySQL，并回调 backend-http

## 上下游依赖

```
Kafka → ugc → 审核模型服务 / 第三方审核接口
           → object-storage（内容/截图拉取）
           → MySQL（审核记录）
           → backend-http（结果回调）
```

## 常见故障模式

| 故障 | 关联 SOP |
|---|---|
| 调审核模型 HTTP 超时/错误 | SOP-F1 / SOP-F2 |
| Kafka 消费滞后 | SOP-F3 |
| DNS 解析审核模型地址失败 | SOP-F6 |
| MySQL 审核记录写入冲突 | SOP-F7 |

## 关键指标

- `ugc_audit_queue_depth`：待审核队列深度
- `ugc_machine_audit_duration_ms_p99`：机审耗时 P99
- `ugc_machine_audit_pass_rate`：机审通过率
- `ugc_human_audit_pending_total`：待人审数量

## 日志特征

- Prefix：`default` / `algo`
- 常见 msg：`content audit submitted` / `model audit result: pass/reject` / `human audit assigned`
- CallerPath 模式：`/service-x/internal/ugc/*.go`

## 配置项

| 配置键 | 含义 | 默认值 | 调整建议 |
|---|---|---:|---|
| `ugc.model.timeout_ms` | 审核模型调用超时 | `10000` | 模型超时后进入降级/人工审核，不建议长时间同步等待 |
| `ugc.human_queue.max_size` | 人审队列上限 | `5000` | 超过上限说明人审能力不足或机审召回过多 |
| `ugc.audit.retry_count` | 审核重试次数 | `2` | 避免重复创建审核任务，重试链路必须幂等 |
