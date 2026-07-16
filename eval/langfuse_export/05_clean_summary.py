import os
from pathlib import Path
_HERE = Path(__file__).resolve().parent
ROOT = _HERE.parents[1]            # 项目根 = eval/langfuse_export 上溯两级
DATA = _HERE / 'data'
DATA.mkdir(exist_ok=True)
"""过滤脏 span（>300s 不可能是真实 worker 执行），重新统计。"""
import json

byrun = json.load(open(str(DATA / "worker_byrun.json")))

runs = [
    ("eval_20260621_065301_e05cad", "Run1 e05cad", "v3历史",       0.593, 0.739, 20, 1),
    ("eval_20260621_075436_d6081b", "Run2 d6081b", "v3历史",       0.755, 0.827, 12, 2),
    ("eval_20260622_013038_995649", "Run3 995649", "v3历史",       0.745, 0.826, 14, 2),
    ("eval_20260622_030712_d729b8", "Run4 d729b8", "v3历史",       0.598, 0.742, 18, 4),
    ("eval_20260625_093200_3bf3ba", "3bf3ba",      "bugfix+硬约束",  0.673, 0.782, 16, None),
    ("eval_20260625_105000_9278f6", "9278f6",      "bugfix+硬约束",  0.709, 0.775, 15, None),
    ("eval_20260626_013852_0584a0", "0584a0",      "bugfix+硬约束",  0.618, 0.761, 17, None),
    ("eval_20260626_030323_083f23", "083f23",      "bugfix无约束",   0.664, 0.776, 13, 0),
    ("eval_20260626_045312_21e367", "21e367",      "v3base基线",    0.655, 0.753, 16, 0),
]

DIRTY = 300.0  # worker 真实执行上限：双超时120s + 多轮LLM，>300s 视为脏 span
print(f"{'run':<12}{'阶段':<13}{'cite':<7}{'total':<7}{'失败':<5}{'报超时':<6}"
      f"{'有效':<5}{'脏':<4}{'60-100':<7}{'100-180':<8}{'>=180':<6}{'卡死%':<7}{'最大':<7}{'均值'}")
for rid, label, stage, cite, total, fails, rto in runs:
    alll = byrun.get(rid, [])
    dirty = sum(1 for x in alll if x > DIRTY)
    lats = sorted(x for x in alll if x <= DIRTY)
    n = len(lats)
    if not n:
        print(f"{label:<12}{stage:<13} 无有效数据"); continue
    b2 = sum(1 for x in lats if 60 <= x < 100)
    b3 = sum(1 for x in lats if 100 <= x < 180)
    b4 = sum(1 for x in lats if x >= 180)
    stuck = (b3 + b4) / n * 100
    mean = sum(lats) / n
    rtos = str(rto) if rto is not None else "?"
    print(f"{label:<12}{stage:<13}{cite:<7}{total:<7}{fails:<5}{rtos:<6}"
          f"{n:<5}{dirty:<4}{b2:<7}{b3:<8}{b4:<6}{stuck:<6.1f}%{max(lats):<7.1f}{mean:.1f}")
