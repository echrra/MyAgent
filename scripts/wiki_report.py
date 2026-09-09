"""知识库链接图报告 —— 断链校验 + 图结构 + 评测集期望文档可达性。

用途（三段，对应三个决策问题）：
  1. 断链清单        —— 手写知识库新增文档时漏建目标文件是高频错误，这里确定性检出。
  2. 图结构统计      —— 孤立节点（入度 0）遍历永远到不了，是 wiki_read 的能力盲区。
  3. expected 可达性 —— 对每条评测 case 的期望引用文档，算「直接入度」和「depth 跳内哪些
     文档可达它」。覆盖率判据取反向可达集（随 --depth 变化），不用只算 1 跳的 in_degree。
     这一段直接回答「wiki_read 值不值得做」：若多数期望文档无来源可达，遍历救不了任何 case。

纯离线：只读 data/docs 与 eval/dataset/cases，不连 DB、不调模型、不调 LLM。

用法：
    uv run python scripts/wiki_report.py
    uv run python scripts/wiki_report.py --depth 2        # 放宽到两跳可达
    uv run python scripts/wiki_report.py --cases E040,E006
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

# scripts/ 不是包；直接 `python scripts/wiki_report.py` 时项目根不在 sys.path。
# 与 ui/chainlit_app.py 同样的处理，保证两种启动方式都能 import opsagent。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from opsagent.core.config import settings  # noqa: E402
from opsagent.core.retrieval.wiki_graph import WikiGraph, build_wiki_graph  # noqa: E402

_CASES_DIR = Path(__file__).resolve().parent.parent / "eval" / "dataset" / "cases"


def _load_expected(case_ids: set[str] | None) -> list[tuple[str, list[str]]]:
    """读评测 case，返回 [(case_id, [expected_doc_id...])]，按 case_id 排序。"""
    out: list[tuple[str, list[str]]] = []
    if not _CASES_DIR.exists():
        print(f"[warn] 评测集目录不存在，跳过第 3 段: {_CASES_DIR}")
        return out
    for path in sorted(_CASES_DIR.glob("*.yaml")):
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            print(f"[warn] 解析失败，跳过 {path.name}: {exc}")
            continue
        cid = str(data.get("id") or path.stem)
        if case_ids and cid not in case_ids:
            continue
        cites = data.get("expected_citations") or []
        if isinstance(cites, str):
            cites = [cites]
        out.append((cid, [str(c) for c in cites]))
    return out


def _report_dangling(g: WikiGraph) -> int:
    """第 1 段：断链清单。返回断链条数。"""
    print("=" * 78)
    print("1. 断链校验（确定性检查，无需 LLM）")
    print("=" * 78)
    dangling = g.dangling()
    if not dangling:
        print("  无断链。")
        return 0
    for lk in dangling:
        src_path = g.docs[lk.src].rel_path
        print(f"  {src_path}:{lk.line}")
        print(f"      → {lk.dst}  （不存在）   锚文本: {lk.anchor}")
    print(f"\n  共 {len(dangling)} 条断链 —— 每条都会让 wiki_read 的这一跳白走。")
    return len(dangling)


def _report_structure(g: WikiGraph) -> None:
    """第 2 段：图结构统计。"""
    print()
    print("=" * 78)
    print("2. 图结构")
    print("=" * 78)
    content_ids = [d for d, m in g.docs.items() if not m.is_index]
    n_content_links = sum(g.out_degree(d) for d in content_ids)
    print(f"  文档总数        : {len(g.docs)}（含目录页 {len(g.docs) - len(content_ids)} 篇）")
    print(f"  内容层链接      : {n_content_links} 条（不含目录页出链）")
    print(f"  断链            : {len(g.dangling())} 条")

    # 孤立节点：内容层入度 0 —— 遍历永远到不了，只能靠向量/BM25 直接命中
    orphans = sorted(d for d in content_ids if g.in_degree(d) == 0)
    print(f"\n  内容层入度为 0 的文档（遍历到不了，{len(orphans)}/{len(content_ids)} 篇）:")
    if orphans:
        for d in orphans:
            print(f"      {g.docs[d].rel_path}")
    else:
        print("      无")

    # 入度分布（按 category 汇总，看哪类文档被引用得多）
    print("\n  按目录汇总（篇数 / 内容层总入度 / 内容层总出度）:")
    by_cat: dict[str, list[int]] = {}
    for d in content_ids:
        row = by_cat.setdefault(g.docs[d].category, [0, 0, 0])
        row[0] += 1
        row[1] += g.in_degree(d)
        row[2] += g.out_degree(d)
    for cat in sorted(by_cat):
        n, ind, outd = by_cat[cat]
        print(f"      {cat:<14} {n:>3} 篇   入度 {ind:>3}   出度 {outd:>3}")


def _report_reachability(g: WikiGraph, depth: int, case_ids: set[str] | None) -> None:
    """第 3 段：评测集期望文档的可达性 —— wiki_read 的收益上限。"""
    print()
    print("=" * 78)
    print(f"3. 评测集期望文档可达性（depth={depth}）")
    print("=" * 78)
    cases = _load_expected(case_ids)
    if not cases:
        return

    n_no_expected = 0     # case 未标期望引用
    n_missing_doc = 0     # 期望文档在知识库里根本不存在
    n_unreachable = 0     # 期望文档存在但 depth 跳内无来源可达（遍历救不了）
    n_reachable = 0       # depth 跳内有来源可达（遍历有机会救）

    # 覆盖率判据用反向可达集，不用 in_degree —— in_degree 只算 1 跳直接入链，
    # 拿它当判据会让 --depth 参数完全不起作用（本脚本此前的 bug）。
    reachable_from = g.reverse_reachable(depth=depth)

    header = f"{depth} 跳内可到达它的来源文档"
    print(f"\n  {'case':<7} {'expected 文档':<42} {'入度':>4} {'可达源':>5}  {header}")
    print("  " + "-" * 80)
    for cid, cites in cases:
        if not cites:
            n_no_expected += 1
            continue
        for doc in cites:
            if not g.has(doc):
                print(f"  {cid:<7} {doc:<42} {'--':>4} {'--':>5}  ！知识库中无此文档")
                n_missing_doc += 1
                continue
            indeg = g.in_degree(doc)       # 1 跳直接入链，仅作诊断展示
            sources = reachable_from[doc]  # depth 跳内可达来源 —— 覆盖率判据
            if not sources:
                n_unreachable += 1
                print(f"  {cid:<7} {doc:<42} {indeg:>4} {0:>5}  （{depth} 跳内无来源可达）")
            else:
                n_reachable += 1
                shown = ", ".join(sources[:3])
                more = f" 等 {len(sources)} 篇" if len(sources) > 3 else ""
                print(f"  {cid:<7} {doc:<42} {indeg:>4} {len(sources):>5}  {shown}{more}")

    total = n_reachable + n_unreachable + n_missing_doc
    print()
    print("  ── 结论 ──")
    print(f"  期望文档条目总数        : {total}")
    print(f"  {depth} 跳内有来源可达       : {n_reachable}")
    print(f"  {depth} 跳内不可达           : {n_unreachable}")
    print(f"  知识库中缺失            : {n_missing_doc}")
    if n_no_expected:
        print(f"  未标 expected_citations : {n_no_expected} 条 case")
    if total:
        pct = 100.0 * n_reachable / total
        print(f"\n  → wiki_read 理论覆盖上限（depth={depth}）：{pct:.1f}% 的期望文档可经链接抵达。")
        print("    （上限而非实测：还取决于检索是否命中了那些来源文档，需在线探针验证。）")
        print("    注：来源集为「全库任意内容文档」时，存在两跳路径的目标必然也存在一跳前驱，")
        print("    故本比例在构造上不随 --depth 变化 —— depth 只放大「可达源」列的数量。")
        print("    要让覆盖率本身随 depth 变化，起点须换成实际检索命中的文档（在线探针）。")


def main() -> int:
    parser = argparse.ArgumentParser(description="知识库链接图报告（纯离线）")
    parser.add_argument("--depth", type=int, default=1, help="可达性判定的最大跳数（默认 1）")
    parser.add_argument("--cases", default="", help="只看这些 case，逗号分隔，如 E040,E006")
    args = parser.parse_args()

    case_ids = {c.strip() for c in args.cases.split(",") if c.strip()} or None

    print(f"知识库目录: {settings.docs_dir}\n")
    graph = build_wiki_graph()
    if not graph.docs:
        print("知识库为空，无法生成报告。")
        return 1

    n_dangling = _report_dangling(graph)
    _report_structure(graph)
    _report_reachability(graph, depth=max(1, args.depth), case_ids=case_ids)

    print()
    # 有断链时以非零码退出，便于将来挂进 CI 做门禁
    return 1 if n_dangling else 0


if __name__ == "__main__":
    raise SystemExit(main())
