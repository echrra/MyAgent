import os
from pathlib import Path
_HERE = Path(__file__).resolve().parent
ROOT = _HERE.parents[1]            # 项目根 = eval/langfuse_export 上溯两级
DATA = _HERE / 'data'
DATA.mkdir(exist_ok=True)
"""一次性拉取本地 Langfuse 的 worker span，按天统计 latency 分布。"""
import json, os, base64, urllib.request, collections

# 从项目 .env 读本地 key
env = {}
with open(ROOT / ".env", encoding="utf-8") as f:
    for line in f:
        line = line.strip()
        if "=" in line and not line.startswith("#"):
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip()

PK = env.get("LANGFUSE_LOCAL_PUBLIC_KEY", "")
SK = env.get("LANGFUSE_LOCAL_SECRET_KEY", "")
auth = base64.b64encode(f"{PK}:{SK}".encode()).decode()
BASE = "http://localhost:3000/api/public/observations"


def fetch(name, page):
    url = f"{BASE}?name={name}&limit=100&page={page}"
    req = urllib.request.Request(url, headers={"Authorization": f"Basic {auth}"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


rows = []
for h in ("worker_h1", "worker_h2", "worker_h3"):
    first = fetch(h, 1)
    pages = first["meta"]["totalPages"]
    print(f"{h}: {pages} pages, total {first['meta']['totalItems']}", flush=True)
    for p in range(1, pages + 1):
        d = first if p == 1 else fetch(h, p)
        for o in d.get("data", []):
            rows.append({"name": o.get("name"), "lat": o.get("latency"),
                         "start": o.get("startTime")})

with open(str(DATA / "workers.jsonl"), "w") as f:
    for r in rows:
        f.write(json.dumps(r) + "\n")

print(f"\n总 worker span: {len(rows)}")
print("=== 按天分布 ===")
c = collections.Counter(r["start"][:10] for r in rows if r["start"])
for day, cnt in sorted(c.items()):
    print(day, cnt)
