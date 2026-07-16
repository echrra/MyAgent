import os
from pathlib import Path
_HERE = Path(__file__).resolve().parent
ROOT = _HERE.parents[1]            # 项目根 = eval/langfuse_export 上溯两级
DATA = _HERE / 'data'
DATA.mkdir(exist_ok=True)
"""拉所有顶层 trace，按 metadata.session_id 提取 run_id 分组，统计 case 级延迟。"""
import json, base64, urllib.request, urllib.parse, collections, re

env = {}
with open(ROOT / ".env", encoding="utf-8") as f:
    for line in f:
        line = line.strip()
        if "=" in line and not line.startswith("#"):
            k, _, v = line.partition("="); env[k.strip()] = v.strip()
auth = base64.b64encode(f"{env['LANGFUSE_LOCAL_PUBLIC_KEY']}:{env['LANGFUSE_LOCAL_SECRET_KEY']}".encode()).decode()

def fetch(page):
    url = f"http://localhost:3000/api/public/traces?limit=100&page={page}"
    req = urllib.request.Request(url, headers={"Authorization": f"Basic {auth}"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)

first = fetch(1)
pages = first["meta"]["totalPages"]
print(f"traces 总页 {pages}, 总数 {first['meta']['totalItems']}", flush=True)

# run_id 正则：eval_YYYYMMDD_HHMMSS_xxxxxx
run_re = re.compile(r"(eval_\d{8}_\d{6}_[0-9a-f]{6})")
recs = []
for p in range(1, pages + 1):
    d = first if p == 1 else fetch(p)
    for t in d.get("data", []):
        meta = t.get("metadata") or {}
        sid = meta.get("session_id", "") if isinstance(meta, dict) else ""
        m = run_re.search(sid or "")
        if m and t.get("name") == "persist_memory":  # persist_memory = 每 case 末尾, 唯一
            recs.append({"run": m.group(1), "lat": t.get("latency") or 0.0,
                         "case": sid.split(m.group(1) + "_")[-1], "ts": t.get("timestamp")})

with open(str(DATA / "traces_byrun.jsonl"), "w") as f:
    for r in recs:
        f.write(json.dumps(r) + "\n")

print(f"持久化 persist_memory trace 数: {len(recs)}")
c = collections.Counter(r["run"] for r in recs)
print("\n各 run 的 case 数（按 run_id）:")
for run, cnt in sorted(c.items()):
    print(f"  {run}  {cnt} cases")
