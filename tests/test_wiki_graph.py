"""wiki_graph 单测 —— 链接解析 / 断链检出 / 遍历语义。

纯内存构造微型语料（tmp_path），不依赖 DB / 向量模型 / LLM，CI 必跑。
末尾一组真语料冒烟测，知识库目录缺失时自动 skip。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from opsagent.core.config import settings
from opsagent.core.retrieval.wiki_graph import (
    INDEX_CATEGORY,
    build_wiki_graph,
)


def _write(base: Path, rel: str, body: str) -> None:
    """在微型语料里写一篇 md（自动建父目录）。"""
    path = base / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


@pytest.fixture
def mini_corpus(tmp_path: Path) -> Path:
    """微型语料：2 篇 sop + 1 篇 pm + 1 篇 runbook + README 目录页。

    链接结构（不含 README）：
        sop-a --> sop-b --> rb-x
        pm-a  --> sop-a
        sop-b --> pm-missing（断链）
    """
    _write(
        tmp_path,
        "sops/sop-a.md",
        "# SOP A 标题\n\n## 相关文档\n\n- [SOP B](sop-b.md)\n",
    )
    _write(
        tmp_path,
        "sops/sop-b.md",
        "# SOP B 标题\n\n## 相关文档\n\n"
        "- [手册 X](../runbooks/rb-x.md)\n"
        "- [PM 不存在](../postmortems/pm-missing.md)\n",
    )
    _write(
        tmp_path,
        "postmortems/pm-a.md",
        "# PM A 标题\n\n## 相关文档\n\n- [SOP A](../sops/sop-a.md)\n",
    )
    _write(tmp_path, "runbooks/rb-x.md", "# 手册 X 标题\n\n正文。\n")
    # 目录页：从库根出发的相对路径，覆盖全库
    _write(
        tmp_path,
        "README.md",
        "# 知识库索引\n\n"
        "| [SOP A](sops/sop-a.md) | 症状甲 |\n"
        "| [SOP B](sops/sop-b.md) | 症状乙 |\n"
        "| [PM A](postmortems/pm-a.md) | 复盘甲 |\n"
        "| [手册 X](runbooks/rb-x.md) | 中间件 |\n",
    )
    return tmp_path


# ---------- 扫描与元数据 ----------


def test_扫到全部文档并解析标题(mini_corpus: Path) -> None:
    g = build_wiki_graph(mini_corpus)
    assert set(g.docs) == {"sop-a", "sop-b", "pm-a", "rb-x", "README"}
    assert g.docs["sop-a"].title == "SOP A 标题"
    assert g.docs["rb-x"].category == "runbooks"


def test_README_标记为目录页(mini_corpus: Path) -> None:
    g = build_wiki_graph(mini_corpus)
    assert g.docs["README"].category == INDEX_CATEGORY
    assert g.docs["README"].is_index
    assert not g.docs["sop-a"].is_index


def test_知识库目录不存在时降级空图(tmp_path: Path) -> None:
    g = build_wiki_graph(tmp_path / "不存在")
    assert g.docs == {}
    assert g.dangling() == []
    assert g.neighbors("任意") == {}


# ---------- 链接解析 ----------


def test_同目录链接可解析(mini_corpus: Path) -> None:
    g = build_wiki_graph(mini_corpus)
    dsts = [lk.dst for lk in g.out_links["sop-a"]]
    assert dsts == ["sop-b"]


def test_跨目录相对链接可解析(mini_corpus: Path) -> None:
    g = build_wiki_graph(mini_corpus)
    dsts = {lk.dst for lk in g.out_links["sop-b"]}
    assert "rb-x" in dsts       # ../runbooks/rb-x.md
    assert "pm-missing" in dsts  # 断链目标也进 out_links（标记而非丢弃）


def test_库根相对链接可解析(mini_corpus: Path) -> None:
    """README 里 `sops/sop-a.md` 这种从库根出发的写法。"""
    g = build_wiki_graph(mini_corpus)
    dsts = {lk.dst for lk in g.out_links["README"]}
    assert dsts == {"sop-a", "sop-b", "pm-a", "rb-x"}


def test_链接带锚文本与行号可溯源(mini_corpus: Path) -> None:
    g = build_wiki_graph(mini_corpus)
    link = g.out_links["pm-a"][0]
    assert link.anchor == "SOP A"
    assert link.line > 0


def test_自引用被忽略(tmp_path: Path) -> None:
    _write(tmp_path, "sops/sop-self.md", "# 自引用\n\n- [自己](sop-self.md)\n")
    g = build_wiki_graph(tmp_path)
    assert g.out_links["sop-self"] == []


def test_越出知识库目录的链接被忽略(tmp_path: Path) -> None:
    _write(tmp_path, "sops/sop-esc.md", "# 越界\n\n- [外部](../../../secret.md)\n")
    g = build_wiki_graph(tmp_path)
    assert g.out_links["sop-esc"] == []


# ---------- 断链 ----------


def test_断链被检出且不进入入链(mini_corpus: Path) -> None:
    g = build_wiki_graph(mini_corpus)
    dangling = g.dangling()
    assert len(dangling) == 1
    assert dangling[0].src == "sop-b"
    assert dangling[0].dst == "pm-missing"
    # 断链目标不存在，不该出现在任何 in_links 里
    assert "pm-missing" not in g.in_links


def test_出度默认不计断链(mini_corpus: Path) -> None:
    g = build_wiki_graph(mini_corpus)
    assert g.out_degree("sop-b") == 1                        # 只有 rb-x
    assert g.out_degree("sop-b", include_dangling=True) == 2  # 含 pm-missing


# ---------- 入度 ----------


def test_入度默认排除目录页(mini_corpus: Path) -> None:
    g = build_wiki_graph(mini_corpus)
    # sop-a 的入链：pm-a（内容）+ README（目录）
    assert g.in_degree("sop-a") == 1
    assert g.in_degree("sop-a", include_index=True) == 2


# ---------- 遍历 ----------


def test_一跳邻居(mini_corpus: Path) -> None:
    g = build_wiki_graph(mini_corpus)
    assert g.neighbors("sop-a", depth=1) == {"sop-b": 1}


def test_两跳邻居累计距离(mini_corpus: Path) -> None:
    g = build_wiki_graph(mini_corpus)
    assert g.neighbors("sop-a", depth=2) == {"sop-b": 1, "rb-x": 2}


def test_遍历默认不经由目录页中转(mini_corpus: Path) -> None:
    """README 链全库；若允许经它中转，任意两点距离都会 ≤2，遍历失去区分度。"""
    g = build_wiki_graph(mini_corpus)
    # rb-x 没有出链，正常情况下从它走不到任何地方
    assert g.neighbors("rb-x", depth=3) == {}
    # 但 pm-a 一跳只应到 sop-a，绝不该因为 README 而摸到 sop-b / rb-x
    assert g.neighbors("pm-a", depth=1) == {"sop-a": 1}


def test_深度为零或起点不存在时返回空(mini_corpus: Path) -> None:
    g = build_wiki_graph(mini_corpus)
    assert g.neighbors("sop-a", depth=0) == {}
    assert g.neighbors("不存在的doc", depth=2) == {}


def test_reaches_目标已在起点集合中记零跳(mini_corpus: Path) -> None:
    g = build_wiki_graph(mini_corpus)
    ok, hops = g.reaches(["sop-b", "pm-a"], "sop-b", depth=1)
    assert ok
    assert hops == 0


def test_reaches_取多起点中的最短跳数(mini_corpus: Path) -> None:
    g = build_wiki_graph(mini_corpus)
    # rb-x：从 sop-b 一跳可达，从 sop-a 两跳可达 → 取 1
    ok, hops = g.reaches(["sop-a", "sop-b"], "rb-x", depth=2)
    assert ok
    assert hops == 1


def test_reaches_不可达返回负一(mini_corpus: Path) -> None:
    g = build_wiki_graph(mini_corpus)
    ok, hops = g.reaches(["rb-x"], "sop-a", depth=3)
    assert not ok
    assert hops == -1


# ---------- 反向可达集（覆盖率判据，必须随 depth 变化）----------


def test_反向可达集一跳(mini_corpus: Path) -> None:
    g = build_wiki_graph(mini_corpus)
    rev = g.reverse_reachable(depth=1)
    assert rev["sop-a"] == ["pm-a"]
    assert rev["sop-b"] == ["sop-a"]
    assert rev["rb-x"] == ["sop-b"]
    assert rev["pm-a"] == []  # 无人链向它 —— 遍历到不了


def test_反向可达集随depth扩大(mini_corpus: Path) -> None:
    """回归锁：覆盖率判据必须随 depth 变化。

    此前 wiki_report 用 in_degree（只算 1 跳）当判据，导致 --depth 2 完全不起作用。
    """
    g = build_wiki_graph(mini_corpus)
    one = g.reverse_reachable(depth=1)
    two = g.reverse_reachable(depth=2)
    # rb-x：一跳只有 sop-b；两跳把 sop-a（sop-a→sop-b→rb-x）也算进来
    assert one["rb-x"] == ["sop-b"]
    assert two["rb-x"] == ["sop-a", "sop-b"]
    # sop-b：两跳把 pm-a（pm-a→sop-a→sop-b）算进来
    assert two["sop-b"] == ["pm-a", "sop-a"]


def test_反向可达集排除目录页作为来源(mini_corpus: Path) -> None:
    """README 链全库；若允许它当来源，所有文档都会显示为可达，覆盖率恒为 100%。"""
    g = build_wiki_graph(mini_corpus)
    rev = g.reverse_reachable(depth=3)
    for srcs in rev.values():
        assert "README" not in srcs


def test_反向可达集覆盖全部文档且深度非法时全空(mini_corpus: Path) -> None:
    g = build_wiki_graph(mini_corpus)
    rev = g.reverse_reachable(depth=0)
    assert set(rev) == set(g.docs)          # key 覆盖全库，调用方不必做 KeyError 防护
    assert all(v == [] for v in rev.values())


# ---------- 真语料冒烟 ----------


@pytest.mark.skipif(
    not Path(settings.docs_dir).exists(),
    reason="知识库目录不存在，跳过真语料冒烟",
)
def test_真语料建图冒烟() -> None:
    g = build_wiki_graph()
    assert len(g.docs) >= 30, "知识库文档数异常偏少"
    # README 必须被识别为目录页（否则遍历会被它污染）
    assert "README" in g.docs
    assert g.docs["README"].is_index
    # 至少要有内容链接（非目录页出链）
    content_links = sum(
        len(v) for k, v in g.out_links.items() if not g.docs[k].is_index
    )
    assert content_links >= 20, "内容层交叉引用数异常偏少"
