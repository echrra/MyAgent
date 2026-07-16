# edgectl-gateway — 入口网关

## 定位

集群唯一入口，所有外部 HTTP 请求经本服务路由到后端。承载限流、鉴权、路由转发、Request ID 注入。

## 关键职责

- **路由转发**：按 path 前缀分流到 backend-http / admin / algo
- **限流**：基于用户 + 接口粒度的令牌桶限流
- **JWT 鉴权**：解析 Header 中 Authorization token，注入 userId/roleKey 到请求上下文
- **Request ID / TraceId 生成**：入口处生成 hex32 TraceId，写入 Response Header + 透传到下游
- **健康检查响应**：`GET /healthz` 直接返回 200（不经过鉴权）

## 上下游依赖

```
Client → gateway → backend-http / admin / algo
                  → configcenter（启动时拉路由表）
```

## 常见故障模式

| 故障 | 关联 SOP |
|---|---|
| 下游超时导致 gateway 返回 504 | SOP-F1 |
| 限流误伤正常用户 | 观察 429 突增 + 用户维度分布 |
| JWT 解析失败（INFO 级陷阱） | SOP-F10 |
| DNS 解析后端服务失败 | SOP-F6 |

## 关键指标

- `http_request_duration_seconds_p99`：按 path 分桶
- `http_request_total{status="429"}`：限流拦截量
- `gateway_upstream_errors_total`：下游返回 5xx 量

## 日志特征

- Prefix：`http`
- 常见 msg：`request started` / `request completed` / `rate limit exceeded`
- 正常 status_code 以 200 为主；异常时出现 429 / 502 / 504

## 配置项

| 配置键 | 含义 | 默认值 | 调整建议 |
|---|---|---:|---|
| `gateway.rate_limit.qps` | 单用户 QPS 上限 | `100` | 按接口类型区分，核心写接口可更低，查询接口可更高 |
| `gateway.upstream_timeout_ms` | 下游超时 | `30000` | 普通接口建议 3~10s，部署/上传等长操作单独配置 |
| `gateway.jwt.secret` | JWT 签名密钥 | `<SECRET>` | 必须和签发方一致，轮换时列出所有签发/解析服务 |
