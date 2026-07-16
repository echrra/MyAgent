# SOP-F2: 下游 HTTP 错误排查

## 适用场景

- 日志出现：`downstream <HOST> returned status=4xx/5xx` / `upstream response errCode=<CODE>`
- 告警：`gateway_upstream_errors_total` 突增
- 用户反馈："操作失败，提示系统错误"

## 排查步骤

### 第 1 步：确认错误分布

查 backend-http 最近调用下游的错误统计：

```
统计条件：service=edgectl-backend-http, level=ERRO, time_window=now-15min~now
关注字段：msg 中的目标 HOST、status_code、errCode
```

### 第 2 步：区分"谁的问题"

这是排查中最关键的判断：

| 错误类型 | 含义 | 责任方 |
|---|---|---|
| HTTP 4xx | 客户端请求有误（参数、权限等）| **调用方排查** |
| HTTP 5xx | 下游服务内部错误 | **下游排查** |
| HTTP 200 + errCode≠0 | 业务层错误（HTTP 层成功，业务失败）| **看 errCode 含义** |

### 第 3 步：HTTP 200 + errCode 陷阱（重点）

**这是最容易漏的故障模式**。日志长这样：

```
level=DEBU status_code=200 msg="Trigger interface error errCode=401"
```

- HTTP 层：200 ✅（监控看是"成功"）
- 业务层：errCode=401（实际是"无权限"）

**排查方法**：不能只看 status_code，要 grep Response body 中的 `errCode` / `errorMsg`。建议排查命令：

```
grep "errCode" /data/logs/backend-http/*.jsonl | jq 'select(.errCode != 0)'
```

### 第 4 步：处置

| 情况 | 处置 |
|---|---|
| 下游 5xx（下游挂了）| 熔断 + 通知下游 oncall |
| 下游 4xx（调错了）| 检查调用参数 / url 拼接 |
| HTTP 200 + errCode≠0 | 按 errCode 含义处理（401→检查权限，403→检查资源）|

## 常见根因

- **下游服务滚动升级**：旧 Pod 终止中，新 Pod 未就绪，连接被拒
- **下游接口变更**：路径/参数格式改了，调用方没更新
- **业务权限变更**：某个角色被回收了权限，但调用方不知道
- **网关层 HTTP 200 包裹业务错误**：这是最常见的漏报原因

## 相关文档

- [SOP-F1: 级联超时排查](sop-f1-cascade-timeout.md)
- [SOP-F10: Token 解码错误](sop-f10-token-decode-error.md)