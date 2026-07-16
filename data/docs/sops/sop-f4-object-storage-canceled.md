# SOP-F4: 对象存储上传失败排查

## 适用场景

- 日志出现：`object storage upload canceled: context canceled` / `put object failed: context canceled`
- 用户反馈："文件上传到一半就失败了" / "固件升级包上传不上去"
- 特征：日志中有 `io copy interrupted at offset=<N>`，说明传输中途被打断

## 排查步骤

### 第 1 步：区分"谁取消了"和"为什么取消"

context canceled 有两个可能来源：

| 来源 | 表现 | 排查方向 |
|---|---|---|
| **上游 HTTP handler ctx 超时** | 上传到一半 ctx deadline exceeded | 网关超时 / 后端处理超时 |
| **用户关闭了页面/连接** | status_code=499，`request canceled while reading body` | 用户行为（不需修复，但要统计量级）|
| **对象存储服务端断开** | 错误来自对象存储 SDK | 检查对象存储服务状态 |

### 第 2 步：检查上传接口的耗时

```
查询：path=/api/v1/<resource>/upload，关注 latency_ms 是否接近网关/上传接口超时阈值
关注：大文件的 latency_ms 是否接近 gateway.upstream_timeout_ms
```

如果上传耗时接近网关超时阈值，说明网关在上传完成前就掐断了连接。

### 第 3 步：检查对象存储服务状态

```
grep "object storage" /data/logs/backend-http/*.jsonl | grep -v canceled
```

排除对象存储本身返回的错误（如 403 权限不足、503 服务不可用）。

### 第 4 步：处置

| 情况 | 处置 |
|---|---|
| 网关超时太短 | 调大 `gateway.upstream_timeout_ms` 或改为异步上传 |
| 用户关闭导致 | 统计量级 + 前端提示"上传中请勿关闭页面" |
| 对象存储服务异常 | 切换到备 Bucket / 联系对象存储运维 |

## 常见根因

- **网关超时 < 上传耗时**：大文件上传应按文件大小和带宽单独设置超时，不能复用普通接口阈值
- **用户提前关闭**：上传界面没有进度条，用户以为卡住了就关页面
- **对象存储限流**：并发上传太多，被对象存储侧限流断开
- **网络不稳定**：客户端到服务器之间丢包

## 与 F1 的关系

F4 和 F1 共享 `context canceled` 主轴。区别在于：

- **F1**：A 调 B，B 超时导致 A 的 ctx 被取消（服务间传导）
- **F4**：上传操作本身超时，ctx 被上游 HTTP handler 取消（用户→服务）

看到 `context canceled` 不要直接按 F1 查，先看 `path`：

- `upload` / `firmware` / `object` 类 path → 优先按 F4 排查
- `deploy` / `work` / `task` 类 path → 优先按 F1 排查

## 相关文档

- [SOP-F1: 级联超时排查](sop-f1-cascade-timeout.md)
- [edgectl-backend-http 服务说明](../services/edgectl-backend-http.md)