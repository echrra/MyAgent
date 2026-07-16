import os
from pathlib import Path
_HERE = Path(__file__).resolve().parent
ROOT = _HERE.parents[1]            # 项目根 = eval/langfuse_export 上溯两级
DATA = _HERE / 'data'
DATA.mkdir(exist_ok=True)
"""建立 traceId→run_id 映射，把 worker span 精确归到每个 run。"""
import json, base64, urllib.request, collections, re

env = {}
with open(ROOT / ".env", encoding="utf-8") as f:
    for line in f:
        line = line.strip()
        if "=" in line and not line.startswith("#"):
            k, _, v = line.partition("="); env[k.strip()] = v.strip()
auth = base64.b64encode(f"{env['LANGFUSE_LOCAL_PUBLIC_KEY']}:{env['LANGFUSE_LOCAL_SECRET_KEY']}".encode()).decode()
run_re = re.compile(r"(eval_\d{8}_\d{6}_[0-9a-f]{6})")

def get(url):
    req = urllib.request.Request(url, headers={"Authorization": f"Basic {auth}"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)

# 1) 拉所有 trace，建 traceId -> run_id
base_t = "http://localhost:3000/api/public/traces"
pages = get(f"{base_t}?limit=100&page=1")["meta"]["totalPages"]
tid2run = {}
for p in range(1, pages + 1):
    d = get(f"{base_t}?limit=100&page={p}")
    for t in d.get("data", []):
        meta = t.get("metadata") or {}
        sid = meta.get("session_id", "") if isinstance(meta, dict) else ""
        m = run_re.search(sid or "")
        if m:
            tid2run[t["id"]] = m.group(1)
print(f"traceId→run 映射: {len(tid2run)} 条", flush=True)

# 2) 拉所有 worker span，按 traceId 归 run
base_o = "http://localhost:3000/api/public/observations"
byrun = collections.defaultdict(list)
unmapped = 0
for h in ("worker_h1", "worker_h2", "worker_h3"):
    pg = get(f"{base_o}?name={h}&limit=100&page=1")["meta"]["totalPages"]
    for p in range(1, pg + 1):
        d = get(f"{base_o}?name={h}&limit=100&page={p}")
        for o in d.get("data", []):
            run = tid2run.get(o.get("traceId"))
            if run:
                byrun[run].append(o.get("latency") or 0.0)
            else:
                unmapped += 1
print(f"worker span 已归类，未映射 {unmapped} 条", flush=True)

out = {run: sorted(lats) for run, lats in byrun.items()}
json.dump(out, open(str(DATA / "worker_byrun.json"), "w"))
print(f"写出 {len(out)} 个 run 的 worker latency")
