# 故障复盘 PM-002：上游接口变更导致 404 雪崩

## 基本信息

| 项目 | 内容 |
|---|---|
| 时间 | 2026-04-02 10:15 ~ 10:45（持续 30 分钟） |
| 影响服务 | backend-http → 外部审核服务 |
| 影响范围 | 设备注册/信息更新功能不可用（需审核设备别名） |
| 触发告警 | P1：ugc_audit_queue_depth 持续增长 |
| 发现人 | 运营反馈"审核队列一堆待处理的，但 alias 审核一直不过" |

## 现象

10:15 开始，设备注册接口返回 `errorCode=500, errorMsg="operation succeeded but action failed"`。日志中大量 `downstream <HOST> returned status=404`，但 gateway 层面看到的 status_code 是 200（包裹陷阱）。

用户在 App 端注册设备时提示"注册成功"但实际上 alias 审核环节失败了（静默失败）。

## 排查过程

| 时间 | 操作 | 发现 |
|---|---|---|
| 10:20 | 查 backend-http 日志 | 大量 `Trigger interface error errCode=500`，但 HTTP status = 200 |
| 10:23 | 按 errCode 过滤 | 返回的 errorMsg 全是"远端不存在" |
| 10:25 | 查下游审核服务的调用日志 | 调用 path 是 `/api/v2/audit/alias` → 返回 404 |
| 10:28 | 联系审核服务团队 | 他们上午 9:00 发版，把 alias 审核接口从 `/api/v2/audit/alias` 改成了 `/api/v1/audit/alias/content`，但**没有通知调用方，也没有做兼容重定向** |
| 10:30 | 确认根因 | 下游接口路径变更，调用方仍用旧路径 → 404 → 被业务包裹成 200+errCode=500 |

## 根因

**直接原因**：审核服务发版修改了接口路径，未做兼容。

**根本原因**：
1. 下游接口变更缺少通知机制（没有 changelog / 没有 API 版本兼容策略）
2. 调用方对下游 4xx 错误没有独立告警——`HTTP 200 + errCode != 0` 这种模式被默认为"业务正常"

## 处置

1. **临时止血**（10:35）：backend-http 紧急修改调用 path 指向新地址，10:45 恢复。
2. **长期方案**：
   - 下游接口增加兼容路由（旧 path → 301 重定向到新 path）
   - 调用方增加 `downstream 4xx` 的独立告警规则（不依赖 status_code）
   - 建立"接口变更知会"流程：发版前通知所有调用方 + 至少保留 1 个版本兼容期

## 教训

1. **"200 不代表成功"是最经典的告警盲区**。HTTP 200 + errCode=500 这种包裹模式在网关上看起来一切正常，但实际业务已经坏了。
2. **接口变更的影响比想象的大**——这次只是一个 path 改动，就导致整条设备注册链路不可用。API 版本管理不是 nice-to-have，是生产必备。

## 关联 SOP

- [SOP-F2: 下游 HTTP 错误排查](../sops/sop-f2-downstream-http-error.md)

## 关联故障模式

F2（下游 HTTP 4xx）