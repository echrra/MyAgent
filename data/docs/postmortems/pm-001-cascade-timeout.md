# 故障复盘 PM-001：调度服务超时导致全链路雪崩

## 基本信息

| 项目 | 内容 |
|---|---|
| 时间 | 2026-03-15 14:32 ~ 14:58（持续 26 分钟） |
| 影响服务 | gateway → backend-http → scheduler |
| 影响范围 | 设备部署功能全部不可用，其他功能正常 |
| 触发告警 | P0：gateway P99 接近网关超时阈值 + 504 比例显著高于基线 |
| 发现人 | 监控告警自动触发 |

## 现象

14:32 开始，`edgectl-gateway` 的部署相关接口（`/api/v1/*/deploy`）P99 延迟从正常的 800ms 飙升到 35s，同时 504 错误率从 0.1% 飙升到 22%。用户反馈"点部署后一直转圈，最后提示部署失败"。

gateway、backend-http、scheduler 三个服务的日志同时出现大量 `context deadline exceeded`。

## 排查过程

| 时间 | 操作 | 发现 |
|---|---|---|
| 14:34 | 查 gateway 的 504 分布 | 只有 `/deploy` 相关 path 异常，其他接口正常 → 排除 gateway 自身问题 |
| 14:36 | 抽一个慢请求的 TraceId 查 Trace | scheduler 的 span duration = 58s（正常应 < 5s），且 scheduler → 设备代理 gRPC 的 span 超时 |
| 14:40 | 查 scheduler 日志 | 大量 `failed to wait release result: ctx done`，gRPC dial timeout |
| 14:43 | 查后端设备代理状态 | 设备端代理进程正常，但 gRPC 端口响应极慢——原来是该区域设备带宽被其他业务占满 |
| 14:50 | 确认根因 | 网络带宽瓶颈导致 scheduler → 设备的 gRPC 调用超时，scheduler 线程池打满，上游 backend-http 等待超时，gateway 继续等待超时 → 三级雪崩 |

## 根因

**直接原因**：设备所在区域的网络带宽被另一个业务的批量日志上传占满，scheduler 到设备的 gRPC 调用全部超时。

**根本原因**：
1. 没有对设备侧 gRPC 调用做资源隔离（线程池与内部调用共享）
2. scheduler 的线程池未设置最大等待队列长度（无界队列）
3. 上游未对 scheduler 调用做熔断（一直等待直到超时）

## 处置

1. **临时止血**（14:55）：手动将 scheduler 的部署任务线程池减半，并重启 scheduler 释放积压线程。部署功能 15:00 恢复。
2. **长期方案**：
   - scheduler 对设备 gRPC 调用使用独立线程池（已完成，3/18 上线）
   - gateway 对 backend-http 加熔断器（按连续失败次数/错误率触发，熔断窗口按恢复速度动态调整）
   - 网络带宽监控告警（按区域 + 业务维度，已加 PromQL 规则）
   - 设备侧 gRPC 调用超时按操作类型分级，避免长时间占住调用链（快速失败优于长时间等待）

## 教训

1. **熔断要加在调用方，不能只依赖超时**：这次所有环节都在等——scheduler 等设备、backend-http 等 scheduler、gateway 等 backend-http。如果任一环节做了熔断，雪崩就会中断。
2. **网络层的故障不会出现在服务日志的"ERROR"里**——gRPC timeout 看起来是服务慢，但根因是带宽。排查时要跳出"服务→DB/缓存"的思维惯性。

## 关联 SOP

- [SOP-F1: 级联超时排查](../sops/sop-f1-cascade-timeout.md)

## 关联故障模式

F1（级联超时）