# SOP-F13: 401 认证失败大规模扩散（Response check failed / socket.io）

## 适用场景

- 日志出现：`Response check failed: 未授权 token: eyJ...` 或 `200 "GET /api/v1/realtime/socket.io" ... token=eyJ...`（level=ERRO/WARN）
- 用户反馈："突然登不上了" / "实时消息断开" / "socket.io 一直连不上"
- 现象：短时间内 401 错误从个位数暴涨到几万级，且**跨多个用户 dept**（不是单账号被封）

## 特征识别

401 大规模扩散与"个别用户 token 过期"完全不同：

| 症状 | 大规模扩散（本 SOP） | 单用户 token 过期 |
|---|---|---|
| 涉及用户数 | 数百-数千 dept | 1 个 |
| 扩散速度 | 分钟级从 0 涨到万 | 无 |
| 一起发生的事件 | 可能有版本发布 / 密钥轮换 / 网关配置变更 | 无 |
| 处置紧急度 | P0（业务受影响） | P3（提示重登即可） |

## 排查步骤

### 第 1 步：确认时间窗与规模

```
统计条件：service=edgectl-backend-http, keyword="Response check failed" OR "401", time_window=1h
关注字段：TraceId, IP, token 中的 iamID
判定：
- 401 数量在 5-15 分钟内涨了一个数量级 → 大规模扩散
- 同时观察 socket.io 401 是否同步涨（长连接受影响面）
```

### 第 2 步：定位共同点

从 401 日志里抽取字段，找**共同点**：

| 共同点 | 可能根因 | 立即验证 |
|---|---|---|
| **所有失败都是同一时刻开始** | JWT 密钥轮换 / 认证中间件配置刷新 | 查那一刻是否有配置变更或重启 |
| **只有特定 dept 失败** | 权限规则配置误改 | 查最近权限组变更 |
| **只有特定 API 路径失败** | 网关路由 / rewrite 规则出错 | 查网关配置变更 |
| **所有旧 token 都失败但新登录能用** | 密钥换了但没广播；或 kid 不匹配 | 检查 JWT header 里的 kid 与后端信任的 kid |
| **连新登录也失败** | 认证服务本身挂了 | 查 auth 服务健康状态 |

### 第 3 步：查最近 30 分钟的变更

**这类故障 90% 有对应的变更事件**：

```
按时间窗查 change_query：service=edgectl-backend-http, service=edgectl-admin, minutes=30
关注：镜像 tag / 配置文件更新 / Secret 更新 / 网关规则更新
```

高危变更清单：
- JWT signing key / kid 更换（session token 全部失效）
- 网关认证 middleware 版本升级
- SSO / IAM 配置刷新（回调 URL 变更）
- 反向代理 header 转发规则变更（token 传递路径丢失）

### 第 4 步：处置

| 场景 | 立即处置 | 后续 |
|---|---|---|
| JWT key 刚轮换，旧 token 全灭 | 通知用户重登；如影响面极大，回滚 key | 修 key 轮换流程，加"双 key 兼容窗口" |
| 权限组配置误改 | 立刻回滚配置 | review 变更审批流程 |
| 网关 rewrite 出错 | 回滚网关配置 | 灰度发布网关规则 |
| 认证服务挂了 | 重启 / 回滚 | 加认证服务健康探针 + 熔断 |
| socket.io 特定问题 | 客户端引导重连；服务端确认 upgrade 头正确转发 | 加 WebSocket 认证监控 |

### 第 5 步：验证恢复

- 看 401 曲线是否掉回基线（正常应该是个位数/分钟）
- 挑几个失败最多的 dept，要求他们重新登录并验证 socket.io 建连成功
- 关注 24h 内是否有二次波动

## 常见根因总结

| 场景 | 一句话 | 责任方 |
|---|---|---|
| JWT key 硬轮换 | 旧 session 一夜失效 | 认证服务运维流程 |
| 认证 middleware 升级引入 bug | 版本回归导致 token 校验路径错 | backend release |
| 网关 header 转发规则变更 | Authorization header 没传到 | 网关配置管理 |
| 密钥/证书文件权限变了 | 后端读不到密钥 | 部署配置 |

## 相关文档

- [SOP-F10: Token decode 错误](sop-f10-token-decode-error.md)（个体解码失败 vs 大规模 401 的区别）
- [PM-013: JWT 密钥轮换后大规模 401](../postmortems/pm-013-jwt-key-rotation-401.md)
