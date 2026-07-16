# edgectl 知识库

> OpsAgent 的数据文档底座。包含服务说明、SOP 排查指南、故障复盘、中间件运维手册四类结构化文档。
> 所有服务名、数据均为虚构（edgectl 是虚构代号），不指向任何真实公司或项目。

> **语料覆盖说明**：知识库与日志语料是两套独立资产，覆盖面有意不同。
> 文档覆盖完整 8 服务拓扑；当前合成日志聚焦 3 个故障高发服务做深——
> edgectl-backend-http、edgectl-admin、edgectl-backend-watcher（共 480 行 / 10 类故障）。
> edgectl-backend-scheduler、edgectl-algo 已在 schema 注册，日志样本作后续增量；
> gateway / ugc / notification 为依赖背景服务，本期不产日志。
> RAG 需要的是完整服务拓扑认知（理解"某错误可能牵涉哪些服务"），故文档广、日志深是刻意切分。

## 目录概览

```
data/docs/
├── README.md              ← 你在这里
├── services/              # 服务说明（8 篇）
├── sops/                  # SOP 排查指南（13 篇）
├── postmortems/           # 故障复盘（12 篇）
└── runbooks/              # 中间件运维手册（4 篇）
```

**总计：37 篇结构化文档**

---

## 一、服务说明（8 篇）

每篇涵盖服务的定位、职责、上下游依赖、常见故障模式、关键指标、配置项。

| 文件 | 服务 | 一句话 |
|---|---|---|
| [edgectl-gateway](services/edgectl-gateway.md) | 入口网关 | 限流、鉴权、路由转发、TraceId 注入 |
| [edgectl-backend-http](services/edgectl-backend-http.md) | 核心 HTTP API | 设备管理、部署、上传、列表查询——故障高发区 |
| [edgectl-backend-watcher](services/edgectl-backend-watcher.md) | 事件监听 / 定时任务 | K8s 事件 watch + 定时对账 + 配置监听 |
| [edgectl-backend-scheduler](services/edgectl-backend-scheduler.md) | 任务调度引擎 | 指令下发、结果轮询——F1 核心参与者 |
| [edgectl-admin](services/edgectl-admin.md) | 后台管控 | 用户管理、运营操作、短信通知 |
| [edgectl-algo](services/edgectl-algo.md) | 异步任务消费 | Kafka 消费、审核、统计——F3 消费端视角 |
| [edgectl-ugc](services/edgectl-ugc.md) | 用户内容审核 | 机审+人审两级流程 |
| [edgectl-notification](services/edgectl-notification.md) | 通知推送 | 多渠道通知下发 |

---

## 二、SOP 排查指南（13 篇）

每篇含适用场景、完整排查步骤、常见根因、处置预案、关联文档。

| 文件 | 故障模式 | 核心排查思路 |
|---|---|---|
| [SOP-F1](sops/sop-f1-cascade-timeout.md) | 级联超时 | 找到"最先慢"的服务 → 分析其根因 → 止损 |
| [SOP-F2](sops/sop-f2-downstream-http-error.md) | 下游 HTTP 错误 | 区分 4xx/5xx/200+errCode 三种模式 |
| [SOP-F3](sops/sop-f3-kafka-error.md) | Kafka 消息队列故障 | 区分生产端失败 vs 消费端滞后 |
| [SOP-F4](sops/sop-f4-object-storage-canceled.md) | 对象存储上传 ctx 取消 | 区分"谁取消的"和"为什么取消" |
| [SOP-F5](sops/sop-f5-sql-ctx-canceled.md) | 数据库慢查询/超时 | 定位慢 SQL → 判断是 SQL 自己慢还是被 ctx 取消 |
| [SOP-F6](sops/sop-f6-dns-lookup-failed.md) | DNS 解析失败 | 区分 no such host / i/o timeout / connection refused |
| [SOP-F7](sops/sop-f7-mysql-business-error.md) | MySQL 业务错误 | 按错误码分类处理：1062/1364/1064 |
| [SOP-F8](sops/sop-f8-configsdk-disconnect.md) | 配置中心 SDK 断连 | 区分 attempt=1 正常重连 vs attempt≥3 持续失败 |
| [SOP-F9](sops/sop-f9-panic-nil-pointer.md) | Go Panic | 提取 panic 类型 → goroutine stack 定位代码行 |
| [SOP-F10](sops/sop-f10-token-decode-error.md) | Token 解码错误（INFO 陷阱） | 按关键词搜不要按级别搜 → 确认密钥一致性 |
| [SOP-F11](sops/sop-f11-requeue-storm.md) | 消息队列 REQUEUE_STORM（任务反复重投） | 区分正常重试 vs 反复重投风暴 |
| [SOP-F12](sops/sop-f12-stuck-task.md) | 任务状态卡住（STUCK_TASK / dropped task） | 定位卡住环节 → 判断是丢任务还是处理挂起 |
| [SOP-F13](sops/sop-f13-401-mass-spread.md) | 401 认证失败大规模扩散 | 从扩散范围反推是密钥/token 还是鉴权链路 |

---

## 三、故障复盘（12 篇）

每篇含时间线、现象、排查过程、根因、处置、教训。

| 文件 | 标题 | 与故障模式对应 |
|---|---|---|
| [PM-001](postmortems/pm-001-cascade-timeout.md) | 调度服务超时导致全链路雪崩 | F1 级联超时 |
| [PM-002](postmortems/pm-002-downstream-http-404.md) | 上游接口变更导致 404 雪崩 | F2 下游 HTTP 错误 |
| [PM-003](postmortems/pm-003-kafka-broker-disk-full.md) | Kafka Broker 磁盘满导致事件丢失 | F3 Kafka 故障 |
| [PM-004](postmortems/pm-004-object-storage-ctx-canceled.md) | 大文件上传被网关超时掐断 | F4 对象存储上传 |
| [PM-005](postmortems/pm-005-slow-sql-join-explosion.md) | 设备列表分页查询打爆数据库 | F5 SQL 超时 |
| [PM-006](postmortems/pm-006-coredns-overload.md) | CoreDNS 过载导致全集群服务间调用大面积失败 | F6 DNS 失效 |
| [PM-007](postmortems/pm-007-duplicate-key-import.md) | 并发设备导入导致 1062 唯一键冲突 | F7 MySQL 业务错误 |
| [PM-008](postmortems/pm-008-configsdk-reconnect-bug.md) | 配置中心滚动升级导致全集群 SDK 断连 | F8 配置中心断连 |
| [PM-009](postmortems/pm-009-nil-pointer-new-device.md) | 数据库返回空结果触发 nil pointer panic | F9 Go Panic |
| [PM-010](postmortems/pm-010-token-key-rotation-silent-failure.md) | Token 密钥轮换导致 INFO 级静默鉴权失败 | F10 Token 解码错误 |
| [PM-011](postmortems/pm-011-distributed-lock-early-expire.md) | 定时任务调度冲突——两个 watcher 实例同时执行对账 | 跨模式（Redis 锁） |
| [PM-012](postmortems/pm-012-ctx-cancel-defer-bug.md) | 跨服务调用链中 context 被意外吞没 | F1 + Go defer 陷阱 |

---

## 四、中间件运维手册（4 篇）

| 文件 | 中间件 | 内容 |
|---|---|---|
| [MySQL](runbooks/mysql.md) | 数据库 | 常用排查命令、关键指标、错误码速查、索引维护、连接池配置 |
| [Kafka](runbooks/kafka.md) | 消息队列 | Topics 清单、关键指标、生产/消费端常见问题和处理、配置参考 |
| [Redis](runbooks/redis.md) | 缓存 | 使用场景、缓存击穿/雪崩/穿透处置、分布式锁问题、常用排查 |
| [Prometheus](runbooks/prometheus.md) | 监控 | 核心告警规则、常用 PromQL、告警分级、Dashboard 推荐面板 |

---

## 文档间的交叉引用关系

```
服务说明 ←→ SOP：每个服务的"常见故障模式"列指向对应 SOP
SOP ←→ 故障复盘：每个 SOP 的"关联文档"指向相关复盘案例
中间件手册 ←→ SOP：手册中的"故障关联"列指向对应 SOP
故障复盘 ←→ SOP：每个复盘标注关联的 SOP 和 Fx 故障模式
```

---

## 语料特点

1. **37 篇结构化文档作为知识库语料**——RAG 检索的底座
2. **文档之间有完整的交叉引用关系**——Agent 查 SOP 时可自动关联历史复盘案例
3. **10 类故障模式每类都有 SOP + 故障复盘**——从排查指南到案例，知识闭环
4. **F10 Token decode error 是 INFO 级告警盲区**——体现"监控失效"类故障的系统思维