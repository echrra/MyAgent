# edgectl-backend-watcher — 后台事件监听 / 定时任务

## 定位

后台常驻服务，负责 K8s 事件监听、定时任务调度、设备状态对账。不直接接收用户请求，通过监听 + 轮询驱动。

## 关键职责

- **K8s 事件监听**：watch 设备 Pod/Deployment 状态变更，更新内部设备状态表
- **定时任务**：
  - `audit_status_sync`：审计日志同步（每 5 分钟）
  - `clean_expired_token`：过期令牌清理（每 30 分钟）
  - `device_state_reconcile`：设备状态对账（每 10 分钟）
- **配置监听**：通过 configsdk 监听配置中心变更

## 上下游依赖

```
backend-watcher → K8s API Server（事件 watch）
                → configcenter（gRPC watch）
                → MySQL（状态写入）
                → Kafka（状态事件发布）
                → Redis（分布式锁）
```

## 常见故障模式

| 故障 | 关联 SOP |
|---|---|
| 配置中心 grpc watch 断连 + 重连 | SOP-F8 |
| Kafka 发布状态变更事件失败 | SOP-F3 |
| MySQL 写入冲突（对账并发） | SOP-F7 |

## 关键指标

- `watcher_event_lag_seconds`：事件处理延迟
- `scheduled_task_duration_ms`：定时任务耗时
- `configsdk_reconnect_total`：配置中心重连次数

## 日志特征

- Prefix：`watcher`、`scheduler`、`default`
- 常见 msg：`event received` / `sync task started` / `configsdk reconnecting`
- WARN 常见：配置中心断连 attempt=1/2
- CallerPath 模式：`/service-x/internal/watcher/*.go` / `/service-x/internal/scheduler/job.go`

## 配置项

| 配置键 | 含义 | 默认值 | 调整建议 |
|---|---|---:|---|
| `watcher.reconcile_interval_ms` | 对账间隔 | `600000` | 对账越频繁 DB 压力越大，按业务实时性和数据量调整 |
| `watcher.audit_sync_batch` | 审计同步批量大小 | `1000` | 批量过大会造成长事务，需结合单批耗时压测 |
| `configsdk.watch_timeout_ms` | 配置 watch 超时 | `30000` | 和配置中心心跳/重连策略配套，不和业务请求超时混用 |
