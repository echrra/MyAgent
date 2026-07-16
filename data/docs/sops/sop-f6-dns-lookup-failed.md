# SOP-F6: DNS 解析 / 服务发现故障排查

## 适用场景

- 日志出现：`dial tcp: lookup <HOST>: no such host` / `DNS lookup failed` / `i/o timeout`
- 告警：某服务的调用方大量 `connection refused` / `unknown host` 错误
- 特征：下游服务自身的指标正常（说明不是下游挂了，而是找不到下游）

## 排查步骤

### 第 1 步：区分 DNS 失败类型

| 错误 | 含义 | 可能原因 |
|---|---|---|
| `no such host` | DNS 解析不到该域名 | 域名被删除 / DNS 记录未同步 / 拼写错误 |
| `i/o timeout` | DNS 服务器无响应 | DNS 服务器挂了 / 网络不通 |
| `connection refused` | DNS 解析成功但端口不通 | 目标服务没启动 / 端口错了 |
| `Temporary failure in name resolution` | DNS 服务器临时不可用 | DNS 服务器过载 / 网络抖动 |

### 第 2 步：检查重试模式

DNS 失败有自动退避重试，看重试的 attempt 变化：

```
grep "retry DNS resolution" /data/logs/*.jsonl | jq '{time, host, attempt, delay}'
```

- **attempt=1/2 后恢复**：瞬时抖动，已自愈，不需要处理
- **attempt 持续上升（≥3）**：持续失败，需要介入
- **所有 attempt 失败**：严重问题，DNS 完全不可达

### 第 3 步：检查服务发现状态

如果使用了 Consul/Nacos 等服务发现：

```
# 检查目标服务是否在注册中心
# 检查服务实例数是否正常
# 检查健康检查是否通过
```

### 第 4 步：处置

| 情况 | 处置 |
|---|---|
| DNS 服务器故障 | 切换备用 DNS / 恢复 DNS 服务（基础设施团队）|
| 目标域名不存在 | 检查域名拼写 / 确认服务是否已下线 |
| 瞬时抖动 | 确认重试机制工作正常，不需人工介入 |
| 网络分区 | 检查安全组/防火墙规则 |

## 常见根因

- **CoreDNS 过载**：集群 DNS 并发请求太多，响应超时
- **DNS 缓存过期**：Pod IP 变了但 DNS 缓存还在用旧记录
- **服务发现组件故障**：Consul/Nacos 挂了，服务列表更新不了
- **网络策略变更**：NetworkPolicy / 安全组规则改了

## 与 F8 的区别

- **F6 DNS 失效**：DNS 解析层面——找不到目标的 IP 地址
- **F8 配置中心断连**：gRPC 连接层面——找到了但连接被关闭

两者会有叠加：配置中心断连后重连时需要重新 DNS 解析，如果此时 DNS 也有问题，恢复时间加倍。

## 相关文档

- [SOP-F8: 配置中心断连排查](sop-f8-configsdk-disconnect.md)
- [PM-006: CoreDNS 过载复盘](../postmortems/pm-006-coredns-overload.md)
- [Prometheus / 指标监控运维手册](../runbooks/prometheus.md)