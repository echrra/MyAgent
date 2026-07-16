"""批量跑 real_cases/ 里的 12 个真实生产 case，检验 agent 在真实 TLS 日志上的诊断质量。

与 test_graph_real_batch.py 的区别：
  - 数据源是「真实云端 TLS」（需 AGENT_ENABLE_TLS_FALLBACK=1 + .env 里的 topic），不是合成 jsonl
  - case 从 real_cases/case_*.md 解析：只取「用户反馈」+ 提取到的 ID + 事发时间线索，
    **不透标题/关键日志/根因**（那些是答案），模拟真实工单入口
  - 无自动 scorer：跑完打印 agent 结论 vs 人工「根因」，人工核对

时间线索：作品 ID 是 ULID，前 10 位 Crockford base32 编码 48bit 毫秒时间戳，
解码出事发时间当查询线索（真实运维手里也有任务创建时间）。
"""

import asyncio
import os
import re
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# 真实 case：显式开云端 fallback（.env 已配 TLS_PROD_TOPIC_ID）
os.environ.setdefault("AGENT_ENABLE_TLS_FALLBACK", "1")

from opsagent.core.graph.builder import get_graph  # noqa: E402
from opsagent.core.graph.state import AgentState  # noqa: E402

_CASES_DIR = Path(__file__).resolve().parents[1] / "real_cases"
_ULID_RE = re.compile(r"(01[0-9A-HJKMNP-TV-Z]{24})")  # ULID 26 位（不要求词边界，兼容 _01K 内嵌）
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_CST = timezone(timedelta(hours=8))  # 北京时间


def _ulid_time(ulid: str) -> datetime | None:
    """解码 ULID 前 10 位为毫秒时间戳 → 北京时间。"""
    try:
        ms = 0
        for ch in ulid[:10]:
            ms = ms * 32 + _CROCKFORD.index(ch)
        return datetime.fromtimestamp(ms / 1000, tz=_CST)
    except (ValueError, OverflowError):
        return None


def _parse_case(path: Path) -> dict:
    """从 md 解析出工单入口信息（用户反馈 + ID + 时间），不含答案。"""
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()

    # 用户反馈：## 用户反馈 段里的 > 引用（可能多行）
    feedback = []
    in_fb = False
    for ln in lines:
        if ln.strip().startswith("## 用户反馈"):
            in_fb = True
            continue
        if in_fb:
            if ln.startswith("## "):
                break
            if ln.strip().startswith(">"):
                feedback.append(ln.strip().lstrip(">").strip())
    feedback_txt = " ".join(f for f in feedback if f) or "（用户反馈：见工单）"

    # ── 基本信息表（| 字段 | 值 |）—— 权威的 TraceID / 时间 / 设备SN，优先于全文正则
    info = {}
    for m in re.finditer(r"^\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|\s*$", text, re.M):
        info[m.group(1).strip()] = m.group(2).strip()

    def _info(*keys):
        norm = {k.replace(" ", "").replace("　", ""): v for k, v in info.items()}
        for q in keys:
            if q.replace(" ", "") in norm:
                return norm[q.replace(" ", "")]
        return ""

    # TraceID：先表格，再全文 trace_id 显式值
    trace_ids = []
    tid = _info("TraceID", "TraceId", "trace_id")
    if tid:
        mt = re.search(r"([0-9a-fA-F]{16,}|01[0-9A-HJKMNP-TV-Z]{24})", tid)
        if mt:
            trace_ids.append(mt.group(1))
    trace_ids += re.findall(r"trace_id[\"'：:\s]+([0-9a-fA-F]{16,}|01[0-9A-HJKMNP-TV-Z]{24})", text)
    trace_ids = list(dict.fromkeys(trace_ids))

    # 事发时间：优先表格『时间』字段（权威，取起始时刻）；缺失才回退 ULID 解码
    incident_time = None
    tstr = _info("时间", "时间点", "发生时间")
    mtime = re.search(r"(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2}):(\d{2})", tstr) if tstr else None
    if mtime:
        y, mo, d, hh, mm, ss = map(int, mtime.groups())
        incident_time = datetime(y, mo, d, hh, mm, ss, tzinfo=_CST)
    ulids = list(dict.fromkeys(_ULID_RE.findall(text)))
    if incident_time is None:
        for u in ulids:
            t = _ulid_time(u)
            if t and 2025 <= t.year <= 2027:
                incident_time = t
                break

    # 用户 ID（含表格里的完整 username）/ 设备 SN
    user_ids = re.findall(r"用户\s+([A-Za-z0-9_一-龥]+(?:\s+[A-Za-z]+)?)", text)
    uid_tbl = _info("用户ID", "用户", "userId")
    if uid_tbl:
        user_ids.insert(0, uid_tbl)
    user_ids = [u for u in dict.fromkeys(user_ids) if u not in {"ID", "userId"}]
    device_sn = _info("设备SN", "SN", "设备")

    # 根因（对照答案，跑完打印）
    root = ""
    m = re.search(r"## 根因\s*\n(.+?)(?:\n## |\Z)", text, re.S)
    if m:
        root = m.group(1).strip()

    return {
        "id": path.stem,
        "title": lines[0].lstrip("# ").strip() if lines else path.stem,
        "feedback": feedback_txt,
        "user_ids": user_ids[:3],
        "ulids": ulids[:6],
        "trace_ids": trace_ids[:3],
        "device_sn": device_sn,
        "incident_time": incident_time,
        "root_cause": root,
    }


def _build_query(c: dict) -> str:
    """拼工单式 query：用户反馈 + 线索，不含根因。TraceId/时间优先。"""
    parts = [f"用户反馈：{c['feedback']}"]
    clues = []
    if c["trace_ids"]:
        clues.append(f"TraceId：{', '.join(c['trace_ids'])}")
    if c["incident_time"]:
        clues.append(f"事发时间：{c['incident_time'].strftime('%Y-%m-%dT%H:%M:%S')}（北京时间）")
    if c["user_ids"]:
        clues.append(f"用户：{', '.join(c['user_ids'])}")
    if c.get("device_sn"):
        clues.append(f"设备SN：{c['device_sn']}")
    if c["ulids"]:
        clues.append(f"作品/任务ID：{', '.join(c['ulids'])}")
    if clues:
        parts.append("线索 —— " + "；".join(clues) + "。")
    parts.append(
        "这是一条真实生产工单，请通过真实日志排查：哪个服务/环节出问题、"
        "用户看到什么错误、根因是什么、要不要处理。查不到就如实说明。"
    )
    return " ".join(parts)


async def _run_one(c: dict) -> dict:
    query = _build_query(c)
    print("=" * 80)
    print(f"▶ [{c['id']}] {c['title'][:70]}")
    print(f"  Query: {query[:150]}...")
    print("-" * 80)

    graph = get_graph()
    initial: AgentState = {
        "session_id": f"realcase-{c['id']}-{int(time.time())}",
        "user_id": f"rc-{c['id']}-{int(time.time())}",  # 每条独立 user，避免 episodic 记忆跨 case 串味
        "user_query": query,
    }
    started = time.perf_counter()
    try:
        final = await graph.ainvoke(initial)
    except Exception as exc:
        print(f"❌ 抛异常：{type(exc).__name__}: {exc}")
        return {"id": c["id"], "elapsed": -1, "tools": 0, "answer": f"[error] {exc}", "root": c["root_cause"]}
    elapsed = time.perf_counter() - started

    wm = final.get("working_memory") or []
    ans = final.get("final_answer") or ""
    print(f"\n📊 耗时 {elapsed:.1f}s / 工具调用 {len(wm)} 次")
    for i, r in enumerate(wm, 1):
        flag = "✓" if r.get("success") else "✗"
        import json
        args = json.dumps(r.get("args") or {}, ensure_ascii=False)[:100]
        src = (r.get("result") or {}).get("meta", {}).get("source", "") if isinstance(r.get("result"), dict) else ""
        print(f"   [{i}] {flag} {r.get('tool_name')}({args}) {r.get('latency_ms')}ms {src}")

    print("\n💬 Agent 结论（前 800 字）:")
    print("   " + ans[:800].replace("\n", "\n   "))
    print("\n🎯 人工根因（对照答案）:")
    print("   " + (c["root_cause"][:400] or "（无根因，可能是查询类）").replace("\n", "\n   "))
    print()
    return {"id": c["id"], "elapsed": elapsed, "tools": len(wm), "answer": ans, "root": c["root_cause"]}


async def _main() -> None:
    only = None
    if len(sys.argv) > 1 and sys.argv[1] == "--only" and len(sys.argv) > 2:
        only = set(sys.argv[2].split(","))

    cases = [_parse_case(p) for p in sorted(_CASES_DIR.glob("case_*.md"))]
    summary = []
    for c in cases:
        if only and c["id"] not in only:
            continue
        r = await _run_one(c)
        summary.append(r)

    print("=" * 80)
    print("总览")
    print("=" * 80)
    for r in summary:
        print(f"  {r['id']}: {r['elapsed']:.1f}s / {r['tools']} 工具")


if __name__ == "__main__":
    asyncio.run(_main())
