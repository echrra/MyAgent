# Langfuse 实证数据导出脚本

从本地自托管 Langfuse（`localhost:3000`）导出评测 run 的 worker span latency，
用于分析 search_sop 卡死 / 超时对 cite 的影响。相关演进背景见
[docs/开发历程.md](../../docs/开发历程.md)（阶段六：cite 双峰根因定位）。

## 前置条件

- 本地 Langfuse 在跑（`make langfuse-up` 或 docker compose）。
- 项目根 `.env` 含 `LANGFUSE_LOCAL_PUBLIC_KEY` / `LANGFUSE_LOCAL_SECRET_KEY`。
- 脚本用标准库（urllib），无需额外依赖；直接 `python3` 或 `.venv/bin/python` 跑。

## 脚本与执行顺序

| 脚本 | 作用 | 输出 |
|------|------|------|
| `01_pull_worker_spans.py` | 拉全部 `worker_h*` span | `data/workers.jsonl` |
| `02_join_by_runid.py` | 建 traceId→run_id 映射，按 run 归类 worker latency | `data/worker_byrun.json` |
| `03_group_traces.py` | 拉全部 trace 按 run_id 分组，统计各 run case 数 | `data/traces_byrun.jsonl` |
| `04_per_run_detail.py` | 打印逐 run 详细数据（分位数/各档/完整序列） | stdout |
| `05_clean_summary.py` | 脏 span 过滤后的汇总对照表 | stdout |
| `06_timeout_estimate.py` | 用「worker ≥150s ≈ 一次 search_sop 双超时」标定尺反推各 run 超时数 | stdout |

依赖关系：`02` 需先跑 `01`（部分版本内部自取，但建议按序）；`04`/`05` 需先跑 `02`。

```bash
cd <项目根>
.venv/bin/python eval/langfuse_export/01_pull_worker_spans.py
.venv/bin/python eval/langfuse_export/02_join_by_runid.py
.venv/bin/python eval/langfuse_export/04_per_run_detail.py   # 逐 run 详细
.venv/bin/python eval/langfuse_export/05_clean_summary.py    # 汇总对照
```

## 关键方法（务必理解）

- **run 精确归属靠 `metadata.session_id`（含 run_id），不靠时间窗**——相邻 run 无空档时时间窗会切错。
- **脏 span 过滤**：`latency > 300s` 视为 span endTime 损坏（跨夜/未正常 close）剔除。
- **卡死判据**：只看 `≥180s worker 数` 和 `case超时数`；100-180s 混了 Semaphore 排队，不是干净的卡死信号。

## 注意

- `04`/`05` 里的 run 清单是**写死的目标 run**（带 cite/total 等报告指标）。分析新 run 时需在脚本顶部 `runs` 列表追加 `(run_id, 标签, 阶段, cite, total, ...)`。
- `data/` 是导出产物目录，可随时重新生成；不必纳入版本管理（如需保留快照可单独提交）。
