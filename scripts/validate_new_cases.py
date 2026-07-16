"""跑新增的 F11/F12/F13 case，验收：
1. search_logs 命中真实 pattern 日志
2. search_sop 命中新写的 SOP
3. 最终诊断报告有正确关键词、无禁忌词
"""
import asyncio
import sys
import time
import yaml
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from opsagent.core.graph.builder import get_graph
from opsagent.core.graph.state import AgentState

CASES = ["E056", "E058", "E060"]


async def _run_one(case_id: str) -> dict:
    yaml_path = Path(__file__).resolve().parents[1] / "eval" / "dataset" / "cases" / f"{case_id}.yaml"
    spec = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))

    print("=" * 78)
    print(f"▶ {case_id} [{spec['fault_pattern']}] {spec['difficulty']}")
    print(f"  Query: {spec['query']}")
    print(f"  期望 SOP: {spec['expected_citations']}")
    print(f"  期望关键词(≥{spec['expected_keywords_min']}): {spec['expected_keywords']}")
    print(f"  禁忌词: {spec['forbidden_keywords']}")
    print("-" * 78)

    graph = get_graph()
    initial: AgentState = {
        "session_id": f"validate-{case_id}-{int(time.time())}",
        "user_id": "validator",
        "user_query": spec["query"],
    }
    started = time.perf_counter()
    final_state = await graph.ainvoke(initial)
    elapsed = time.perf_counter() - started

    working_memory = final_state.get("working_memory") or []
    final_answer = final_state.get("final_answer") or ""

    # 命中检查
    ans_lower = final_answer.lower()
    hit_kws = [k for k in spec["expected_keywords"] if k.lower() in ans_lower]
    forbidden_hit = [k for k in spec["forbidden_keywords"] if k.lower() in ans_lower]
    # SOP 命中：doc id 完整、或 fault_id（F11/F12/F13）出现在 answer（应对 answer 用标题格式引用）
    sop_hit = []
    for c in spec["expected_citations"]:
        cl = c.lower()
        if cl in ans_lower:
            sop_hit.append(c)
            continue
        parts = cl.split("-")
        fault_id = parts[1] if len(parts) > 1 else ""
        if fault_id and (f"sop-{fault_id}" in ans_lower or f"sop {fault_id}" in ans_lower or f"sop-{fault_id.upper()}" in final_answer):
            sop_hit.append(c)

    tool_calls = [(r.get("tool_name"), r.get("args") or r.get("tool_args") or {}) for r in working_memory]

    print(f"\n📊 耗时 {elapsed:.1f}s / 工具调用 {len(tool_calls)} 次")
    for i, (t, a) in enumerate(tool_calls, 1):
        args_summary = ", ".join(f"{k}={v!r}" for k, v in list(a.items())[:3])
        print(f"   [{i}] {t}({args_summary[:100]})")

    print(f"\n💬 最终答案（前 500 字）：\n   {final_answer[:500]}...")

    print(f"\n✅ 验收：")
    print(f"   期望关键词命中：{len(hit_kws)}/{spec['expected_keywords_min']}  {hit_kws}")
    print(f"   期望 SOP 命中: {sop_hit or '❌ 未命中'}")
    print(f"   禁忌词误触: {forbidden_hit or '✓ 无'}")

    return {
        "case_id": case_id,
        "elapsed": elapsed,
        "keywords_hit": len(hit_kws),
        "keywords_min": spec["expected_keywords_min"],
        "sop_hit": bool(sop_hit),
        "forbidden_hit": len(forbidden_hit),
        "answer_preview": final_answer[:200],
    }


async def main():
    results = []
    for cid in CASES:
        try:
            r = await _run_one(cid)
            results.append(r)
        except Exception as e:
            print(f"\n❌ {cid} 抛异常：{type(e).__name__}: {e}")
            results.append({"case_id": cid, "error": str(e)})
    print("\n" + "=" * 78)
    print("总览")
    print("=" * 78)
    for r in results:
        if "error" in r:
            print(f"  ❌ {r['case_id']}: ERROR {r['error']}")
        else:
            kw_ok = "✓" if r["keywords_hit"] >= r["keywords_min"] else "✗"
            sop_ok = "✓" if r["sop_hit"] else "✗"
            fb_ok = "✓" if r["forbidden_hit"] == 0 else "✗"
            print(f"  {r['case_id']}: {r['elapsed']:>5.1f}s  keywords {kw_ok}({r['keywords_hit']}/{r['keywords_min']})  sop {sop_ok}  forbidden {fb_ok}")


if __name__ == "__main__":
    asyncio.run(main())
