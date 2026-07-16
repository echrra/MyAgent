# SOP-F10: Token 解码错误（INFO 级别陷阱）

## 适用场景

- 日志出现：`decode error` / `[LcToken] decode error Token: <TOKEN>` / `base64 decode failed` / `verification failed`
- 特征：日志级别是 **INFO**，不是 ERRO——这是关键陷阱
- 用户反馈："登录失败了" / "扫码后没反应" / "token 验证不过"
- 现象：监控上看错误率没涨（因为只设了 ERRO 级别告警），但用户大量反馈失败

## 为什么这是"陷阱"

```
Level: INFO       ← 坏就坏在这
msg: [LcToken] decode error Token: <TOKEN> base64.StdEncoding.Decode failed
status_code: 200  ← 网关也看不出来
```

**告警规则通常只监控 Level=ERRO 的日志**。这条日志是 INFO 级别，告警完全看不到。但实际影响是：用户认证失败，功能不可用。

## 排查步骤

### 第 1 步：按关键词搜，不要按级别搜

```
# 错误做法（漏报）
grep '"Level":"ERRO"' /data/logs/*.jsonl  # ← 找不到

# 正确做法
grep "decode error" /data/logs/*.jsonl
grep "verification failed" /data/logs/*.jsonl
grep "illegal base64 data" /data/logs/*.jsonl
```

### 第 2 步：分析 token 为什么解不出来

| 错误 | 可能原因 |
|---|---|
| `base64.StdEncoding.Decode failed` | Token 格式不对——不是标准 base64 |
| 输入的 base64 长度不对 | Token 被截断（URL 中 `+` 变空格等） |
| `token signature mismatch` | 密钥不匹配——可能是新旧密钥轮换导致 |
| `token expired` | 过期（反而不是陷阱，是正常逻辑） |

### 第 3 步：确认影响范围

查单位时间内的 decode error 量级变化：

```
统计：msg 含 "decode error" 的 INFO 日志，按 5min 窗口聚合
关注：量级突增的时间点，是否与以下事件重合：
  - 客户端版本发布
  - Token 生成逻辑变更
  - 密钥轮换操作
```

### 第 4 步：处置

| 情况 | 处置 |
|---|---|
| token 格式异常（大量） | 检查客户端 token 生成逻辑 |
| 密钥不匹配 | 确认密钥轮换是否完成，新旧密钥是否都在配发中 |
| 偶发单条 | 可能是用户拿旧 token / 复制粘贴截断，可以忽略 |

## 告警规则修正（重要）

**建议加一条关键词告警**：

```
告警条件：msg 正则匹配 "decode error|verification failed" 的 INFO 日志
按历史基线设置阈值；可先用“1 分钟内超过 50 条”作为初始值，后续按正常误报率调整
```

这样即使日志级别是 INFO 也不会漏。

## 常见根因

- **客户端 URL 编码问题**：token 中 `+` 被浏览器变成空格
- **密钥轮换期间新旧密钥并存**：部分 token 用旧密钥签发，但服务端已切新密钥
- **客户端 SDK 版本不兼容**：token 生成格式变了
- **复制粘贴截断**：用户从邮件/短信里复制 token 时漏了最后几个字符

## 为什么这个案例值得强调

这是生产实践中最容易被忽视的一类故障——"告警盲区"，它体现了几点系统性认知：
1. 告警不能只看 ERROR 级别
2. `status_code=200` 不代表业务成功
3. "告警规则设计"需要系统性思考

## 相关文档

- [SOP-F2: 下游 HTTP 错误排查](sop-f2-downstream-http-error.md)（共享 200+errCode 陷阱）
- [edgectl-gateway 服务说明](../services/edgectl-gateway.md)