import os
from pathlib import Path
_HERE = Path(__file__).resolve().parent
ROOT = _HERE.parents[1]            # 项目根 = eval/langfuse_export 上溯两级
DATA = _HERE / 'data'
DATA.mkdir(exist_ok=True)
"""为 11-md 生成极详细的 per-run 原始数据：分位数、各档、完整延迟序列。"""
import json

byrun = json.load(open(str(DATA / "worker_byrun.json")))

runs = [
    ("eval_20260621_065301_e05cad", "Run1 e05cad", "v3历史·低峰",   0.593, 0.739, 1.000, 0.799, 18.5, 20, 1),
    ("eval_20260621_075436_d6081b", "Run2 d6081b", "v3历史·高峰",   0.755, 0.827, 0.991, 0.858, 17.0, 12, 2),
    ("eval_20260622_013038_995649", "Run3 995649", "v3历史·高峰",   0.745, 0.826, 0.972, 0.852, 13.2, 14, 2),
    ("eval_20260622_030712_d729b8", "Run4 d729b8", "v3历史·低峰",   0.598, 0.742, 0.990, 0.794, 15.7, 18, 4),
    ("eval_20260625_093200_3bf3ba", "3bf3ba",      "bugfix+硬约束", 0.673, 0.782, 0.982, 0.000, 0.0, 16, 0),
    ("eval_20260625_105000_9278f6", "9278f6",      "bugfix+硬约束", 0.709, 0.775, 0.991, 0.000, 0.0, 15, 0),
    ("eval_20260626_013852_0584a0", "0584a0",      "bugfix+硬约束", 0.618, 0.761, 0.973, 0.000, 0.0, 17, 0),
    ("eval_20260626_030323_083f23", "083f23",      "bugfix·无约束", 0.664, 0.776, 1.000, 0.818, 18.2, 13, 0),
    ("eval_20260626_045312_21e367", "21e367",      "v3base·纯基线", 0.655, 0.753, 1.000, 0.767, 14.5, 16, 0),
]

def pct(s, p):
    if not s: return 0.0
    return s[min(len(s)-1, int(len(s)*p))]

DIRTY = 300.0
for rid, label, stage, cite, total, f1, conc, forb, fails, rto in runs:
    raw = sorted(byrun.get(rid, []))
    clean = [x for x in raw if x <= DIRTY]
    dirty = len(raw) - len(clean)
    s = clean
    n = len(s)
    print(f"### {label}  ({rid})")
    print(f"阶段={stage} | cite={cite} total={total} F1={f1} conc={conc} forbidden={forb}% 失败case={fails} case超时={rto}")
    if not n:
        print("  worker span: 无\n"); continue
    buckets = {
        "<30s":sum(1 for x in s if x<30),"30-60s":sum(1 for x in s if 30<=x<60),
        "60-90s":sum(1 for x in s if 60<=x<90),"90-120s":sum(1 for x in s if 90<=x<120),
        "120-150s":sum(1 for x in s if 120<=x<150),"150-180s":sum(1 for x in s if 150<=x<180),
        ">=180s":sum(1 for x in s if x>=180)}
    print(f"  worker span: 原始{len(raw)} 有效{n} 脏(>300s){dirty}")
    print(f"  min={s[0]:.1f} P25={pct(s,.25):.1f} P50={pct(s,.5):.1f} P75={pct(s,.75):.1f} P90={pct(s,.9):.1f} P95={pct(s,.95):.1f} max={s[-1]:.1f} mean={sum(s)/n:.1f}")
    print(f"  档位: " + " ".join(f"{k}={v}" for k,v in buckets.items()))
    print(f"  原始latency(秒,升序,有效): {[round(x,1) for x in s]}")
    print()
