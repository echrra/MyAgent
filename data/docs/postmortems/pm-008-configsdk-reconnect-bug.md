# 故障复盘 PM-008：配置中心滚动升级导致全集群 SDK 断连

## 基本信息

| 项目 | 内容 |
|---|---|
| 时间 | 2026-06-01 02:00 ~ 02:15（持续 15 分钟，影响延续到早晨） |
| 影响服务 | 所有 edgectl-* 服务（均依赖 configcenter） |
| 影响范围 | 服务正常运行但配置热更新失效 15 分钟。早 8 点运营修改了限流阈值发现不生效。 |
| 触发告警 | P2：configsdk_reconnect_total 凌晨 2:00-2:15 突增 |
| 发现人 | 监控告警（凌晨未处理） + 运营早晨反馈（"改了限流值没效果"） |

## 现象

凌晨 2:00，配置中心开始滚动升级。每个配置中心实例 graceful_stop 时关闭 gRPC watch 流，所有客户端的 configsdk 收到 `received prior goaway: graceful_stop`。

所有 edgectl 服务日志出现：
```
WARN: configsdk: watch disconnected, reconnecting group=<GROUP> attempt=1 delay=4s
WARN: configsdk: watch disconnected, reconnecting group=<GROUP> attempt=2 delay=8s
```

然后 2:08 所有 attempt≤2 的 SDK 都重连成功：`configsdk: reconnect successful group=<GROUP> after 2 attempts`。

**看起来没问题**——SDK 自动重连了，服务没挂。P2 告警凌晨没人在意。

第二天上午 8:30，运营修改了限流配置（需要紧急生效），发现改了快 30 分钟，实际限流还是旧值。

## 排查过程

| 时间 | 操作 | 发现 |
|---|---|---|
| 08:35 | 运营反馈后排查 | 查 configcenter 日志——配置确实接收到了运营的修改 |
| 08:40 | 查 configcenter 的推送记录 | 新配置在 08:05 推送到所有客户端了——推送成功 |
| 08:45 | 查一个 backend-http 实例 | 它的本地缓存仍然是旧值——它没有收到推送 |
| 08:50 | 发现关键 | 凌晨 2:08 重连后，有 3 个服务实例的 watch 流是**单向的**——SDK 连上了，但 gRPC watch 流没有正确重建。配置中心能推，但客户端收不到。 |
| 08:55 | 确认根因 | configcenter SDK 的重连逻辑有 bug：重连时 `Reconnect()` 建立了 gRPC 连接但忘了调用 `Watch()` 重建 watch 流。这个 bug 在凌晨的滚动升级中被触发。 |

## 根因

**直接原因**：configcenter SDK 的 `Reconnect()` 方法只重建了 gRPC 连接，没有同步重建 watch 流 → 客户端"连上了但没在听"。

**根本原因**：
1. SDK 的 Reconnect 逻辑和初始 Watch 逻辑是分开的两段代码，重连时遗漏了 Watch 调用
2. 这个 bug 在上次配置中心升级前就存在，但上次升级是全部重启（不是滚动），所有客户端 Watch 都重新建立了，没暴露
3. 缺乏"配置版本号一致性"的校验——服务端推送了配置，但客户端从未确认它收到了

## 处置

1. **临时止血**（09:00）：重启受影响的 3 个服务实例，Watch 流正常建立，配置生效。
2. **长期方案**：
   - 紧急修复 SDK `Reconnect()` 中缺失的 `Watch()` 调用
   - SDK 增加健康检查：每 60s 向配置中心确认"我收到的配置版本号"，如果与中心版本不一致，主动重建 Watch
   - 配置中心增加"推送确认"机制——推送后 30s 客户端未 ACK 则重推 + 告警

## 教训

1. **"重连成功"不代表"功能正常"**——gRPC 连接建立和业务 Watch 流是两层。运维排查时要区分。
2. **滚动升级暴露的 bug 往往比全量重启更隐蔽**——全量重启时所有客户端都重新 Watch，bug 被"掩盖"了。
3. **配置变更如果长时间不生效，服务还在用旧配置运行**——这是最危险的沉默故障之一。应该加"配置版本号一致性"监控。

## 关联 SOP

- [SOP-F8: 配置中心断连排查](../sops/sop-f8-configsdk-disconnect.md)

## 关联故障模式

F8（配置中心 SDK 断连）