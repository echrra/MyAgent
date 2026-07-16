# edgectl-backend-http — 核心 HTTP API

## 定位

业务侧主流量入口，承载设备管理、工作流部署、控制指令下发、文件上传等核心 REST API。是 10 类故障中多数故障的发生地。

## 关键职责

- **设备管理 CRUD**：设备注册、信息查询、状态同步
- **工作流部署**：创建部署任务 → 调用 scheduler 下发 → 轮询结果
- **控制指令下发**：单设备 / 批量设备指令（重启、升级、配置变更）
- **文件上传**：固件包 / 配置模板上传到对象存储
- **列表查询**：设备列表、工作流列表、事件日志分页查询（涉及复杂 SQL）

## 上下游依赖

```
gateway → backend-http → backend-scheduler（部署/指令下发）
                       → backend-watcher（事件查询）
                       → object-storage（文件上传）
                       → MySQL（CRUD）
                       → Kafka（事件发布）
                       → configcenter（配置读取）
```

## 常见故障模式

| 故障 | 关联 SOP |
|---|---|
| 调 scheduler 超时 → ctx deadline exceeded | SOP-F1 |
| 调外部 HTTP 接口返回 4xx/5xx | SOP-F2 |
| Kafka 发布事件失败 + 重试 | SOP-F3 |
| 对象存储上传被 ctx 取消 | SOP-F4 |
| 列表查询 SQL 超时被取消 | SOP-F5 |
| MySQL 唯一键冲突 / 字段缺失 | SOP-F7 |
| nil pointer panic | SOP-F9 |
| Token 解码失败（INFO 陷阱） | SOP-F10 |

## 关键指标

- `http_request_duration_seconds_p99`：按 path + method 分桶
- `http_request_total{status="500"}`：5xx 错误率
- `deploy_task_failed_total`：部署失败量
- `upload_bytes_total`：上传流量

## 日志特征

- Prefix：`http`（请求生命周期）、`database`（SQL 跟踪）、`default`（业务逻辑）
- CallerPath 模式：`/service-x/internal/controller/*.go` / `/service-x/internal/service/*.go`
- ERRO 日志含 Content 多行体（Error + Stack 栈帧）

## 配置项

| 配置键 | 含义 | 默认值 | 调整建议 |
|---|---|---:|---|
| `backend.deploy.timeout_ms` | 部署等待超时 | `60000` | 长操作不要占住普通 HTTP 链路，必要时改异步任务 + 轮询 |
| `backend.upload.max_size_mb` | 上传文件大小上限 | `500` | 需和网关超时、对象存储限制、前端上传体验一起压测 |
| `backend.db.query_timeout_ms` | 数据库查询超时 | `30000` | 普通查询建议更短（1~3s），复杂报表/导出建议异步化 |
