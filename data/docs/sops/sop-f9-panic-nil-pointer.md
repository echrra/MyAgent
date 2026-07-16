# SOP-F9: Go Panic 排查指南

## 适用场景

- 日志出现：`runtime error: invalid memory address or nil pointer dereference` / `panic: <REASON>` / `goroutine <N> [running]`
- 告警：服务 500 错误率突增，但服务进程没挂（被 recover 中间件兜住了）
- 用户反馈："突然报 500 错误，刷新又好了"

## 排查步骤

### 第 1 步：确认不是服务挂了

Go 的 panic 如果被 recover 捕获，进程不会挂——只是当前请求返回 500。所以：
- 👍 被 recover 兜住的 panic：服务还活着，单次请求失败
- 💀 没被 recover 的 panic：整个进程崩溃重启

从日志看：如果有 `recovered from panic in <HANDLER>` 字样 → 被兜住了。

### 第 2 步：提取 panic 类型和位置

从 ERRO 日志的 Content/Stack 中提取关键信息：

```
grep "panic:" /data/logs/*.jsonl | jq '{time, msg, Content}'
```

常见 panic 类型：

| Panic 关键词 | 含义 | 常见原因 |
|---|---|---|
| `nil pointer dereference` | 用了空指针 | 没判 nil 就调方法/取字段 |
| `index out of range` | 数组越界 | 没有检查 len 就取下标 |
| `send on closed channel` | 往已关闭的 channel 发消息 | 并发问题，channel 生命周期管理错误 |
| `invalid memory address` | 内存访问错误 | 同 nil pointer |

### 第 3 步：从 goroutine stack 定位代码行

panic 日志的 goroutine stack 会打印完整调用链：

```
goroutine 183 [running]:
  handler/deploy.go:186     ← 这里触发了 nil pointer
  service/release.go:142    ← 调用了 release 服务
  middleware/recover.go:38  ← recover 中间件兜住了
```

**第一个不是 runtime 的调用**就是触发 panic 的代码行。

### 第 4 步：分析触发条件

查该 TraceId 的完整请求链路：
- 请求走了什么 path？
- 带了什么参数？
- 是否是特定数据才触发？（比如某个字段为空时才 panic）

### 第 5 步：处置

| 情况 | 处置 |
|---|---|
| 偶发 nil pointer | 查 null 数据来源 → 加判空 / 加默认值 |
| 特定输入触发 | 加参数校验 → 对异常输入返回 4xx 而不是 panic |
| 频繁 panic | 紧急回滚 / Hotfix → 线上代码有严重 bug |
| channel 相关 panic | 审查并发代码的 channel 关闭时机 |

## 常见根因

- **数据库查不到数据返回 nil**：`db.Find(&obj)` 没找到，obj 是零值，然后 `obj.Field` 就 panic
- **JSON 反序列化不完整**：某个字段缺失导致后续使用 nil
- **map 取值不判断 ok**：`m[key]` 返回零值但 key 不存在
- **并发读写 map**：Go 的 map 不是并发安全的，并发写会 panic

## 相关文档

- [edgectl-backend-http 服务说明](../services/edgectl-backend-http.md)