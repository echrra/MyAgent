# Redis 运维手册

## 适用服务

edgectl-backend-http（缓存设备信息/配置）、edgectl-backend-watcher（分布式锁——定时任务互斥）

## 使用场景

| 场景 | Key 模式 | 默认过期策略 | 补充说明 |
|---|---|---|---|
| 设备信息缓存 | `device:<SN>` | TTL `300s`（5min），LRU 淘汰 | 建议加随机抖动，避免同批 key 同时过期 |
| 配置热缓存 | `config:<KEY>` | 配置中心推送刷新时主动删除 | 配置类缓存不只依赖 TTL，重点看推送一致性 |
| 定时任务分布式锁 | `lock:cron:<TASK>` | TTL `60s`（任务执行期间持有） | 长任务建议 TTL ≥ 历史 P99 × 1.5，并支持续期 |
| 用户登录状态 | `session:<UID>` | TTL `7200s`（2h） | 按安全策略调整，敏感系统可缩短 |
| 限流计数器 | `ratelimit:<UID>:<API>` | TTL `60s`，滑动窗口 | 窗口大小按接口 QPS 策略设置 |

## 关键指标

| 指标 | 含义 | 默认告警阈值 | 补充判断 |
|---|---|---|---|
| `redis_hit_ratio` | 缓存命中率 | `< 80%` | 如果同时 DB QPS/慢查询上升，说明可能已穿透到 DB |
| `redis_connected_clients` | 当前连接数 | `> 80% of maxclients` | >70% 预警，>85% 且连接失败需升级 |
| `redis_used_memory_pct` | 内存使用率 | `> 80%` | >90% 或出现关键 key 淘汰需升级 |
| `redis_evicted_keys_total` | 淘汰 key 量 | 持续增长 → 内存不足 | 结合命中率下降判断影响 |
| `redis_keyspace_misses_total` | 未命中次数 | 突增 → 缓存穿透 | 与历史基线对比，避免误判正常流量增长 |

## 常见问题和处理

### 缓存击穿（热点 key 过期）

**现象**：某个热点 key 过期瞬间，大量请求穿透到 DB，DB QPS 飙升。

**临时止血**：
- 热点 key 手动设置较长 TTL 或临时不过期
- DB 侧加限流

**根治**：
- 互斥锁加载：只有一个请求去查 DB 并回写缓存，其他等待
- 异步刷新：key 过期前提前异步刷新

### 缓存雪崩（大量 key 同时过期）

**现象**：同一时刻大量 key 过期，DB 瞬间被打爆。

**预防**：
- TTL 加随机偏移（如 `300s + random(0, 60s)`）
- 对非关键缓存设置不同的 TTL 阶梯

### 缓存穿透（查不存在的数据）

**现象**：大量查询不存在的 key（如恶意构造的随机 ID），每次穿透到 DB 都查不到。

**预防**：
- 空值也缓存（TTL 短一些，如 `60s`）
- 布隆过滤器（在查 DB 前先判断 key 是否可能存在）

### 分布式锁问题

**现象**：锁持有者挂了，锁没释放，其他定时任务实例无法获取锁；或任务未结束但锁提前过期，导致并发执行。

**预防**：
- 锁必须设 TTL（不能永不过期）
- TTL > 任务最长执行时间（给足 buffer）
- 长任务需要续期机制，续期失败应主动停止任务
- 任务本身必须幂等，不能只依赖锁保证正确性

### 内存不足

**现象**：`redis_evicted_keys_total` 持续增长，命中率下降。

**处置**：
- 检查是否有 key 忘了设 TTL（用 `SCAN` 找无 TTL 的 key）
- 检查是否有异常大 key（`redis-cli --bigkeys`）
- 扩容内存 / 调整淘汰策略

## 常用排查命令

```bash
# 查看内存使用
redis-cli INFO memory

# 查看 key 数量 + 命中率
redis-cli INFO stats

# 查看慢查询
redis-cli SLOWLOG GET 10

# 查看大 key
redis-cli --bigkeys

# 查看某个 key 的 TTL
redis-cli TTL device:<SN>

# 查看当前连接
redis-cli CLIENT LIST
```

## 故障关联

- 缓存穿透 → 大量请求打 DB → DB 慢 → SOP-F5
- 分布式锁未释放/提前过期 → 定时任务堆积或并发执行 → SOP-F1 / PM-011
