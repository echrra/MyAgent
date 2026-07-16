# 故障复盘 PM-012：跨服务调用链中 context 被意外吞没

## 基本信息

| 项目 | 内容 |
|---|---|
| 时间 | 2026-05-28 19:10 ~ 19:40（持续 30 分钟） |
| 影响服务 | backend-http → scheduler → 设备代理 |
| 影响范围 | 批量设备升级任务中约 15% 的设备"看起来成功了但实际没升级" |
| 触发告警 | 无自动告警——运维在例行数据对账时发现 |
| 发现人 | 第二天 data team 发现"已升级设备数 ≠ 日志记录数" |

## 现象

运维在后台发起了一个批量升级任务（200 台设备）。任务完成后统计显示：198 台"成功"，2 台"失败"。看起来正常。

但第二天数据团队发现：有 28 台设备的实际版本号没有变更。其中 26 台在系统里标记为"成功"，但实际上没升级。

## 排查过程

| 时间 | 操作 | 发现 |
|---|---|---|
| Day2 10:00 | 发现数据异常 | 数据团队定位到有 28 台设备"状态标记为成功但版本号没变" |
| 10:30 | 反查 scheduler 日志 | 28 台设备中 26 台的 scheduler 日志里没有 `task completed`，也没有 `task failed` |
| 10:45 | 查 backend-http 日志 | 这 26 个任务在 backend-http 侧的状态是"sent to scheduler"，没有后续 |
| 11:00 | 关键发现 | backend-http 调 scheduler 的 gRPC 调用中，scheduler 收到了请求，执行了一半，然后 ctx 被取消了。scheduler 的 recover 逻辑吞了 context canceled 错误，把任务状态留在了"pending" |
| 11:15 | 查为什么 ctx 被取消 | backend-http 的批量任务 for 循环中，defer cancel() 的作用域有问题——在处理完第 10 台设备后提前调用了 cancel()，导致后续 gRPC 调用的 ctx 都是 canceled 状态 |
| 11:20 | 确认根因 | 代码 bug：defer 放在 for 循环内部而不是单次迭代的函数作用域内，导致 cancel 的时机不对 |

## 根因

**直接原因**：backend-http 批量任务代码中，`defer cancel()` 的作用域错误，导致 ctx 被意外提前取消。

**根本原因**：
1. Go 的 `defer` 在 for 循环中不会在每次迭代结束时执行，而是在函数返回时才执行——开发者混淆了这个行为
2. 任务状态的"pending"在统计时被当成了"异常但可恢复"，没有触发告警
3. scheduler 收到 canceled ctx 后只是吞了错误，没有向上汇报

## 处置

1. **代码修复**：把 ctx 创建 + defer cancel() 的逻辑包在匿名函数中，每次迭代独立创建和取消。
2. **数据修复**：28 台设备重新下发升级任务。
3. **长期方案**：
   - Lint 规则：for 循环中的 defer 告警
   - scheduler 增加"pending 超时"告警：任务状态超过该任务类型的历史 P99 耗时仍未结束 → 自动标记失败 + 告警
   - 批量任务的进度统计增加"pending 数量"维度

## 教训

1. **Go 的 `defer` 在 for 循环中是经典陷阱**——defer 在函数结束时才执行，不是在 for 的每次迭代结束。应该用匿名函数包裹。
   ```go
   // 错误 ❌
   for _, item := range items {
       ctx, cancel := context.WithTimeout(parent, timeout)
       defer cancel()  // 只在整个函数结束时执行！
       process(ctx, item)
   }
   // 正确 ✅
   for _, item := range items {
       func() {
           ctx, cancel := context.WithTimeout(parent, timeout)
           defer cancel()  // 在匿名函数结束时执行 ✓
           process(ctx, item)
       }()
   }
   ```
2. **"pending"不是"正常的中间状态"**——所有中间状态都应该有超时兜底。如果任务长时间处于 pending，应该触发告警而非静默。

## 关联 SOP

- [SOP-F1: 级联超时排查](../sops/sop-f1-cascade-timeout.md)（context canceled 主轴）

## 关联故障模式

F1（级联超时）+ 跨模式（Go defer 陷阱）