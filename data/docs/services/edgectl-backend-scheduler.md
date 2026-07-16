# edgectl-backend-scheduler — 任务调度引擎

## 定位

控制指令的调度执行引擎。接收 backend-http 提交的部署/控制任务，负责分发给设备端并跟踪执行结果。是 F1（级联超时）的核心参与者。

## 关键职责

- **任务入队**：接收部署/控制任务，写入任务表
- **任务分发**：通过 gRPC 下发给边缘设备 / 设备代理
- **结果轮询**：等待设备侧执行结果并更新状态
- **重试逻辑**：网络瞬时失败自动重试（最多 3 次）
- **状态对账**：定期扫描 pending/running 任务，补偿状态

## 上下游依赖

```
backend-http → scheduler → 边缘设备 / 设备代理（gRPC）
                        → MySQL（任务状态持久化）
                        → Kafka（任务事件发布）
```

## 常见故障模式

| 故障 | 关联 SOP |
|---|---|
| 设备端响应慢 → ctx timeout → 级联上游超时 | SOP-F1 |
| gRPC 连接失败（设备离线） | SOP-F2 |
| MySQL 任务状态更新冲突 | SOP-F7 |
| nil pointer（任务对象为空） | SOP-F9 |

## 关键指标

- `scheduler.task_queue_depth`：任务队列深度
- `scheduler.task_execution_duration_ms_p99`：任务执行耗时 P99
- `scheduler.task_timeout_total`：超时任务量
- `scheduler.grpc_dial_failures_total`：gRPC 连接失败量

## 日志特征

- Prefix：`scheduler`
- 关键 msg：`task dispatched` / `task completed` / `task timeout` / `rota release result`
- ERRO 特征（F1）：`failed to wait release result: ctx done` / `context deadline exceeded`
- CallerPath 模式：`/service-x/internal/scheduler/*.go` / `/service-x/internal/grpc/dial.go`

## 配置项

| 配置键 | 含义 | 默认值 | 调整建议 |
|---|---|---:|---|
| `scheduler.task_timeout_ms` | 单个任务超时 | `60000` | 按任务类型拆分，不同设备/网络环境可配置不同超时 |
| `scheduler.max_retries` | 最大重试次数 | `3` | 必须配合指数退避和幂等，避免重试放大故障 |
| `scheduler.queue_capacity` | 队列容量 | `10000` | 不要无界队列；容量应按消费速率和可接受排队时长计算 |
| `scheduler.grpc_dial_timeout_ms` | gRPC 连接超时 | `5000` | 通常 1~5s，跨地域或弱网设备单独配置 |
