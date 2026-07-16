# 故障复盘 PM-010：Token 密钥轮换导致 INFO 级静默鉴权失败

## 基本信息

| 项目 | 内容 |
|---|---|
| 时间 | 2026-03-28 16:00 ~ 16:45（持续 45 分钟才被发现） |
| 影响服务 | gateway（JWT 鉴权中间件） |
| 影响范围 | 约 30% 用户登录后操作提示"未授权"，反复重新登录无效 |
| 触发告警 | **无任何告警**——故障持续 45 分钟全靠用户反馈才发现 |
| 发现人 | 用户群反馈"扫码登录后一直提示请重新登录" |

## 现象

16:00，运维按计划执行了 JWT 签名密钥的轮换操作。操作步骤为：
1. 生成新密钥对
2. 更新 gateway 配置中 `gateway.jwt.secret` 为新密钥
3. 重启 gateway

完成后，旧 token 开始逐步失效。但部分用户登录后拿到的 token 仍然无法通过鉴权。

gateway 日志中开始出现（关键：Level = INFO！）：
```
INFO: [LcToken] decode error Token: <TOKEN> base64.StdEncoding.Decode failed: illegal base64 data at input byte 120
INFO: decode error
```

**没有任何 ERROR 级别的日志**，所有 status_code 都是 200（网关包裹），`http_request_total{status="5xx"}` 没有波动。

## 排查过程

| 时间 | 操作 | 发现 |
|---|---|---|
| 16:20 | 用户群反馈增多 | 运维开始排查 |
| 16:22 | 查 gateway 的 ERRO 日志 | **没有 ERRO** |
| 16:25 | 查 gateway 的 5xx 指标 | **没有 5xx 增长** |
| 16:30 | 查看最近变更 | 16:00 做了密钥轮换——怀疑方向 |
| 16:32 | 回看 gateway 的 INFO 日志 | 发现了大量 `decode error`，16:00 开始 |
| 16:35 | 分析错误 token | 新旧两种格式混在一起——现象是：某些服务还在用旧密钥签发 token，gateway 已经切了新密钥 |
| 16:38 | 确认根因 | 密钥轮换操作只更新了 gateway，但 admin 和 backend-http 内部的 token 生成用的是一个独立的密钥配置项，那个没更新 |

## 根因

**直接原因**：密钥轮换时只改了 gateway 的解密密钥，没改 admin/backend-http 的签发密钥，导致两边的密钥不一致。

**根本原因**：
1. JWT 密钥散落在 3 个服务的配置中（gateway 解密 + admin 签发 + backend-http 签发），没有集中管理
2. 密钥轮换 SOP 没有列出"所有用这个密钥的服务"
3. 鉴权失败被当作"正常的业务事件"打了 INFO 级别，告警系统基于 Level=ERRO 过滤，完全漏过
4. Token 解码失败后返回的 HTTP 200 + errCode=401 又绕过了 5xx 告警——**双重告警盲区**

## 处置

1. **临时止血**（16:40）：把新密钥同步更新到 admin 和 backend-http，重启生效，16:45 恢复。
2. **长期方案**：
   - JWT 密钥集中管理：从 configcenter 读取，所有服务读同一个配置项
   - 密钥轮换 SOP 增加"影响范围检查清单"
   - **关键词告警**：增加 `msg 含 "decode error"` 的 INFO 日志告警，阈值按历史基线设置，初期可用固定值试运行后再调优
   - Token decode 失败的日志级别从 INFO 改为 WARN（保留不给 ERRO 以免噪音过大）

## 教训

1. **"INFO 级别的故障"是运维最怕的情况**——因为所有的告警规则都默认过滤 INFO，这种故障会一直静默到用户投诉。
2. **密钥轮换不是改一个配置项就完事**——要先确认"所有签发/解密密钥的地方"，列清单，改完后全量验证。
3. **双层告警盲区（Level=INFO + status_code=200）是最经典的"监控失效"模式**——是这类故障中很有代表性的案例。

## 关联 SOP

- [SOP-F10: Token 解码错误排查](../sops/sop-f10-token-decode-error.md)

## 关联故障模式

F10（Token decode error）