# edgectl-notification — 通知推送服务

## 定位

统一通知服务，负责短信、邮件、Webhook 等多渠道消息发送。上游只提交通知事件，由本服务处理模板渲染、渠道路由、失败重试。

## 关键职责

- **Kafka 消费**：消费 `notification-event` topic
- **模板渲染**：根据通知类型渲染短信/邮件/Webhook 内容
- **多渠道发送**：短信、邮件、Webhook
- **发送重试**：渠道发送失败自动重试（指数退避）
- **发送记录**：写入 MySQL，便于追踪和对账

## 上下游依赖

```
Kafka → notification → SMS Gateway（外部）
                     → Email Service（外部）
                     → Webhook URLs（外部回调）
                     → MySQL（发送记录）
```

## 常见故障模式

| 故障 | 关联 SOP |
|---|---|
| 调外部短信网关超时/错误 | SOP-F1 / SOP-F2 |
| Kafka 消费滞后 | SOP-F3 |
| DNS 解析外部网关地址失败 | SOP-F6 |
| MySQL 发送记录写入冲突 | SOP-F7 |

## 关键指标

- `notification_send_total`：按渠道 + 状态分桶
- `notification_latency_ms_p99`：发送延迟 P99
- `notification_retry_total`：重试量

## 日志特征

- Prefix：`default` / `http`
- 常见 msg：`notification sent channel=<CH>` / `notification failed retry=<N>` / `template rendered`
- CallerPath 模式：`/service-x/internal/notification/*.go`

## 配置项

| 配置键 | 含义 | 默认值 | 调整建议 |
|---|---|---:|---|
| `notification.sms.timeout_ms` | 短信网关超时 | `5000` | 外部供应商接口通常不宜等待过久，失败后走重试 |
| `notification.max_retries` | 最大重试次数 | `3` | 避免无限重试刷爆渠道，需配合幂等和去重 |
| `notification.retry_backoff_ms` | 重试退避基数 | `1000` | 建议指数退避 + jitter，避免同一时刻重试风暴 |
