# 故障复盘 PM-009：数据库返回空结果触发 nil pointer panic

## 基本信息

| 项目 | 内容 |
|---|---|
| 时间 | 2026-04-18 11:08 ~ 11:12（持续 4 分钟） |
| 影响服务 | backend-http |
| 影响范围 | 设备详情查询接口 500 错误率从 0 → 8%，其他接口正常 |
| 触发告警 | P0：500 错误率突增 |
| 发现人 | 监控告警 |

## 现象

11:08 开始，`/api/v1/devices/{SN}/detail` 接口的 500 错误率从 0 跳升到 8%。日志出现：

```
ERRO: runtime error: invalid memory address or nil pointer dereference
recovered from panic in handler/detail.go:186
```

panic 被 recover 中间件捕获，服务进程没挂。但受影响的请求全返回 500。

## 排查过程

| 时间 | 操作 | 发现 |
|---|---|---|
| 11:09 | 告警触发 | 立即看 backend-http 日志 |
| 11:10 | 提取 panic stack | `handler/detail.go:186` → `device.LatestDeployWork.Status` |
| 11:11 | 查 bug 原因 | `device.LatestDeployWork` 是 nil，代码没有判空直接取 `.Status` |
| 11:11 | 查为什么 LatestDeployWork 是 nil | 当天上午 10:50 有一批设备刚注册，还没有任何部署记录 → `LatestDeployWork` 为 nil |
| 11:12 | 确认根因 | 对新注册设备调 detail 接口 → 数据库查不到 LatestDeployWork → 代码没判空 → nil pointer → panic |

## 根因

**直接原因**：`device.LatestDeployWork` 对新注册设备为 nil，代码直接取 `.Status` 触发 nil pointer。

**根本原因**：
1. JOIN 查询使用 LEFT JOIN，返回值中关联表的字段可以为 NULL——但 Go 的 ORM 把 NULL 映射成了 nil 指针，而业务代码没有判空
2. 这个接口之前只被老设备调用（老设备都有部署记录），新设备注册场景没被测试到
3. 代码 Review 时没有关注"如果 JOIN 结果为空会怎样"的边界条件

## 处置

1. **临时止血**（11:12）：Hotfix 加 nil 检查，`if device.LatestDeployWork != nil { ... }`，11:15 上线。
2. **长期方案**：
   - 全面扫描所有 LEFT JOIN 的查询结果——对关联表的字段加强制 nil 检查
   - 对"新设备无部署记录"的情况设计合理的默认返回值（如 `status: "none"`）
   - 单元测试补上"关联数据为空"的 case

## 教训

1. **LEFT JOIN + nil pointer 是 Go 开发最常见的 bug 模式**——数据库的 NULL 映射到 Go 的 nil，而 Go 不会帮你自动判空。
2. **recover 中间件是救命稻草，但不是遮羞布**——这次 panic 被兜住了，服务没挂，但不能因为有了 recover 就不修 nil pointer。
3. **"新数据的状态"是测试盲区**——老设备没问题不代表新设备没问题。测试数据应该覆盖"刚创建、还没任何关联记录"的状态。

## 关联 SOP

- [SOP-F9: Go Panic 排查](../sops/sop-f9-panic-nil-pointer.md)

## 关联故障模式

F9（Go panic / nil pointer）