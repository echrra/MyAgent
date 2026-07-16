# 故障复盘 PM-006：CoreDNS 过载导致集群内服务间调用大面积失败

## 基本信息

| 项目 | 内容 |
|---|---|
| 时间 | 2026-04-25 22:10 ~ 22:35（持续 25 分钟） |
| 影响服务 | 所有 edgectl-* 服务 |
| 影响范围 | 所有服务间调用出现间歇性 `unknown host` 错误 |
| 触发告警 | P0：多个服务同时出现 502/503 错误率突增 |
| 发现人 | 监控告警 |

## 现象

22:10 开始，多个服务同时出现 `dial tcp: lookup <HOST>: i/o timeout` 和 `no such host` 错误。调用链表现为：

- gateway → backend-http：间歇性 `502 Bad Gateway`
- backend-http → scheduler：`dns lookup failed` + 重试 `attempt=1/2/3`
- algo → backend-http（审核回调）：大量失败

关键观察：**所有受影响服务的自身指标（CPU、内存、QPS）都正常**——说明不是服务挂了，是"找不到服务"。

## 排查过程

| 时间 | 操作 | 发现 |
|---|---|---|
| 22:13 | 查受影响服务日志 | 全是 DNS 类错误（`lookup <HOST>: i/o timeout`） |
| 22:15 | 检查各服务 Pod | 所有 Pod Running —— 排除服务挂 |
| 22:17 | 测试 DNS 解析 | `nslookup edgectl-backend-http` 在 Pod 内执行，耗时 5s 才返回——DNS 慢 |
| 22:20 | 查 CoreDNS Pod | CoreDNS 的 CPU 使用率 95%，`coredns_dns_requests_total` 飙升到正常值的 10 倍 |
| 22:22 | 查 CoreDNS 日志 | 大量来自某爬虫服务的 DNS 查询（每秒 5000+ 次），该服务在做外部域名批量解析 |
| 22:25 | 确认根因 | 同一个集群内的爬虫服务疯狂查询外部域名，打爆了 CoreDNS，导致所有服务的 DNS 解析都受影响 |

## 根因

**直接原因**：同一个 K8s 集群内的爬虫服务产生了大量外部 DNS 查询，CoreDNS CPU 过载，影响全集群 DNS 解析。

**根本原因**：
1. CoreDNS 未设资源 limit（CPU 被无限制使用）
2. CoreDNS 没有做按 namespace 的 QPS 限制
3. 爬虫服务应该用自己的 DNS 解析器（不应依赖集群 CoreDNS 做大量外部解析）

## 处置

1. **临时止血**（22:25）：
   - 对爬虫服务的 Pod 加 NetworkPolicy 限制 DNS 请求频率
   - CoreDNS 扩容从 2 副本到 4 副本
   - 22:35 DNS 恢复正常
2. **长期方案**：
   - CoreDNS 设置 CPU limit + HPA 自动扩缩
   - 爬虫服务改为使用外部 DNS 服务器（8.8.8.8）而非集群 CoreDNS
   - 增加 CoreDNS 的监控延迟告警（`coredns_dns_request_duration_seconds_p99 > 1s`）

## 教训

1. **共享基础设施的"邻居效应"（Again）**——和 Kafka 事件一样，这次是 DNS。共享组件上的一个租户可以拖垮所有人。
2. **DNS 慢了，所有服务调用都慢**——微服务架构中 DNS 是隐藏的"基础设施瓶颈"，平时被忽略，出事就是全局性的。
3. **"服务自己没挂"不代表故障不在自己这边**——本次所有服务指标正常，但 DNS 层出问题，排查时要跳出"服务=进程"的思维框。

## 关联 SOP

- [SOP-F6: DNS 解析故障排查](../sops/sop-f6-dns-lookup-failed.md)

## 关联故障模式

F6（DNS 解析失败 + 重试）