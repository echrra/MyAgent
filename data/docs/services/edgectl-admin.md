# edgectl-admin — 后台管控服务

## 定位

面向运营/管理员的后台服务，提供用户管理、设备批量操作、数据导出、通知触发等能力。流量不大，但操作权限高，错误影响面大。

## 关键职责

- **用户/角色管理**：管理员、部门、角色权限
- **批量操作**：批量导入设备、批量修改状态、批量下发任务
- **数据导出**：运营报表、设备列表、事件日志导出
- **短信通知**：通过第三方短信网关发送告警/通知（F11 预留）

## 上下游依赖

```
gateway → admin → MySQL（用户/运营数据）
                → backend-http（复用部分业务能力）
                → SMS Gateway（短信发送，外部）
                → object-storage（导出文件存储）
```

## 常见故障模式

| 故障 | 关联 SOP |
|---|---|
| 列表查询 SQL 超时 | SOP-F5 |
| 下游 HTTP 错误（调 backend-http 失败） | SOP-F2 |
| MySQL 唯一键冲突（用户导入） | SOP-F7 |
| Token 解码错误（INFO 陷阱） | SOP-F10 |

## 关键指标

- `admin_operation_duration_ms`：运营操作耗时
- `admin_export_rows_total`：数据导出行数
- `admin_sms_send_total`：短信发送量

## 日志特征

- Prefix：`http`（请求）、`database`（SQL）、`default`（业务逻辑）
- 常见 msg：`user list query` / `batch operation completed` / `export task finished`
- 业务事件：`event_action=user_import` / `event_action=batch_device_op`

## 配置项

| 配置键 | 含义 | 默认值 | 调整建议 |
|---|---|---:|---|
| `admin.export.max_rows` | 单次导出最大行数 | `50000` | 大导出建议异步化，避免占住 HTTP 和 DB 连接 |
| `admin.batch_op.max_devices` | 批量操作最大设备数 | `1000` | 超过上限应拆批或进入异步任务队列 |
| `admin.sms.retry_count` | 短信重试次数 | `2` | 重试需配合退避和去重，避免重复通知 |
