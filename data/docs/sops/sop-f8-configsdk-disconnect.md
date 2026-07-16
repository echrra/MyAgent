# SOP-F8: 配置中心 SDK 断连排查

## 适用场景

- 日志出现：`configsdk: watch disconnected, reconnecting group=<GROUP> attempt=<N> delay=<N>s`
- grpc 错误：`closing transport due to: connection error` / `received prior goaway: graceful_stop`
- 行为：服务没有挂，但配置不会热更新了——用的是上次拉到的缓存值

## 排查步骤

### 第 1 步：判断严重程度

**关键**：不是每条 `disconnected` 都需要告警。区分：

| 信号 | 含义 | 行动 |
|---|---|---|
| attempt=1, 随后 `reconnect successful` | 配置中心滚动升级导致的正常重连 | 不需要处理 |
| attempt=1, 随后 `reconnect successful` after 2 attempts | 快速恢复，延迟 4s | 不需要处理 |
| attempt≥3, 没有 `reconnect successful` | 持续失败 | **需要介入** |

### 第 2 步：确认配置中心状态

```
# 检查配置中心服务是否存活
# 检查配置中心的 grpc 端口是否可达
# 检查配置中心是否有最近的变更/升级
```

### 第 3 步：检查退避重连是否在预期范围内

正常的重连退避序列（内置指数退避）：

```
attempt=1 delay=4s
attempt=2 delay=8s
attempt=3 delay=16s
```

如果退避序列符合预期，且最终 reconnect successful，说明 SDK 工作正常，是配置中心侧的正常重启。

### 第 4 步：检查"无配置热更新"的影响

即使 SDK 断连，服务仍在使用**上一次拉到的配置**运行——不会 crash。但以下场景有影响：

- 紧急修改了限流/熔断配置 → **不会生效**
- 紧急修改了路由规则 → **不会生效**
- 非紧急的日常配置变更 → 影响较小

### 第 5 步：处置

| 情况 | 处置 |
|---|---|
| 配置中心滚动升级导致 | 观察，确认 reconnect 后恢复 |
| 持续无法重连 | 检查配置中心服务 + 网络 |
| 配置变更紧急需要生效 | 手动重启相关服务（迫使重新拉配置） |

## 常见根因

- **配置中心滚动升级**：最常见的触发原因。旧实例 graceful_stop 时关闭 grpc 流，SDK 自动重连到新实例
- **网络策略变更**：配置中心的 grpc 端口被防火墙挡了
- **配置中心负载高**：大量客户端同时重连，配置中心处理不过来
- **证书过期**：grpc TLS 证书过期，连接被拒绝

## 相关文档

- [SOP-F6: DNS 解析故障排查](sop-f6-dns-lookup-failed.md)
- [edgectl-backend-watcher 服务说明](../services/edgectl-backend-watcher.md)