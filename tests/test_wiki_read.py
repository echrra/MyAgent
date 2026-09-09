"""wiki_read 单测 —— ID 归一化 / section 切分 / 候选建议 + 真语料行为。

分两层：
- 纯函数层（_normalize_doc_id / _split_h2 / _suggest_doc_ids）无任何依赖，CI 必跑。
- 真语料层验证 wiki_read 的端到端行为（读页面 / 出链 / 候选建议 / section 过滤），
  知识库目录或基准文档缺失时自动 skip，不连 DB、不调模型、不调 LLM。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from opsagent.core.config import settings
from opsagent.core.tools.real import (
    _WIKI_READ_MAX_DOCS,
    _normalize_doc_id,
    _split_h2,
    _suggest_doc_ids,
    wiki_read,
)

# 真语料测试的基准文档：出链丰富（SOP + 服务说明 + 复盘），适合验证遍历
_ANCHOR_DOC = "sop-f8-configsdk-disconnect"


def _corpus_ready() -> bool:
    """知识库存在且基准文档在位。"""
    base = Path(settings.docs_dir)
    return base.exists() and any(base.rglob(f"{_ANCHOR_DOC}.md"))


requires_corpus = pytest.mark.skipif(
    not _corpus_ready(), reason="知识库或基准文档缺失，跳过真语料测试"
)


# ---------- doc_id 归一化 ----------


@pytest.mark.parametrize(
    "raw",
    [
        "sop-f8-configsdk-disconnect",
        "  sop-f8-configsdk-disconnect  ",
        "sop-f8-configsdk-disconnect.md",
        "sop-f8-configsdk-disconnect.MD",
        "sops/sop-f8-configsdk-disconnect.md",
        "../sops/sop-f8-configsdk-disconnect.md",
        "data\\docs\\sops\\sop-f8-configsdk-disconnect.md",
        "[sop-f8-configsdk-disconnect]",
        "'sop-f8-configsdk-disconnect'",
    ],
)
def test_归一化吃掉路径与后缀(raw: str) -> None:
    """LLM 常把 rel_path 或带引号的写法当 doc_id 传进来，都要能识别。"""
    assert _normalize_doc_id(raw) == "sop-f8-configsdk-disconnect"


def test_归一化空串安全():
    assert _normalize_doc_id("   ") == ""


# ---------- section 切分 ----------


def test_切分_无二级标题时整篇一段() -> None:
    text = "# 标题\n\n正文一行。\n"
    assert _split_h2(text) == [("", text)]


def test_切分_前言与各段() -> None:
    text = "# 大标题\n\n开场白。\n\n## 第一段\n\n甲。\n\n## 第二段\n\n乙。\n"
    out = _split_h2(text)
    titles = [t for t, _ in out]
    assert titles == ["", "第一段", "第二段"]
    # 前言不含任何 ## 行
    assert "##" not in out[0][1]
    # 段正文保留自己的标题行，便于直接喂给 LLM 看清上下文
    assert out[1][1].startswith("## 第一段")
    assert "乙" in out[2][1]


def test_切分_首行即二级标题时无前言项() -> None:
    out = _split_h2("## 唯一段\n\n内容。\n")
    assert [t for t, _ in out] == ["唯一段"]


# ---------- 候选建议 ----------


def test_建议_子串匹配优先() -> None:
    known = ["sop-f8-configsdk-disconnect", "pm-008-configsdk-reconnect-bug", "sop-f1-x"]
    assert _suggest_doc_ids("configsdk", known) == [
        "pm-008-configsdk-reconnect-bug",
        "sop-f8-configsdk-disconnect",
    ]


def test_建议_近似匹配兜底() -> None:
    """完全没有子串关系时退到 difflib，救 LLM 的拼写错误。"""
    known = ["sop-f8-configsdk-disconnect", "pm-001-cascade-timeout"]
    got = _suggest_doc_ids("pm-001-cascade-timout", known)  # 少一个 e
    assert got == ["pm-001-cascade-timeout"]


def test_建议_毫不相干时返回空() -> None:
    assert _suggest_doc_ids("zzzzzzzzzz", ["sop-f1-x", "pm-001-y"]) == []


# ---------- wiki_read 真语料行为 ----------


@requires_corpus
def test_读一篇返回正文与元数据() -> None:
    out = wiki_read(doc_ids=_ANCHOR_DOC)
    assert out["meta"]["found"] == 1
    assert out["meta"]["not_found"] == []
    doc = out["data"][0]
    assert doc["doc_id"] == _ANCHOR_DOC
    assert doc["title"]                      # H1 解析出来了
    assert doc["category"] == "sops"
    assert doc["rel_path"].endswith(".md")
    assert doc["sections"]                   # 至少有一个二级标题
    assert len(doc["content"]) > 100         # 全文而非片段


@requires_corpus
def test_出链结构完整且带目标标题() -> None:
    """出链必须带目标标题 —— Agent 才能判断这一跳值不值得走。"""
    out = wiki_read(doc_ids=_ANCHOR_DOC)
    links = out["data"][0]["links"]
    assert links, "基准文档应有交叉引用出链"
    for lk in links:
        assert set(lk) == {"doc_id", "title", "anchor", "dangling"}
        assert lk["anchor"]                  # 锚文本非空
        if not lk["dangling"]:
            assert lk["title"], "非断链目标必须能取到标题"


@requires_corpus
def test_出链覆盖到复盘_锁住SOP到PM的反向链接() -> None:
    """回归锁：SOP→PM 反向链接是把复盘从图上的孤儿救回来的关键，不能被删掉。"""
    out = wiki_read(doc_ids=_ANCHOR_DOC)
    dsts = [lk["doc_id"] for lk in out["data"][0]["links"]]
    assert any(d.startswith("pm-") for d in dsts), "基准 SOP 应能顺出链走到对应复盘"


@requires_corpus
def test_不存在的ID给候选建议而非硬失败() -> None:
    bad = "sop-f8-configsdk-disconnec"  # 末尾少一个 t
    out = wiki_read(doc_ids=bad)
    assert out["data"] == []
    assert out["meta"]["not_found"] == [bad]
    assert _ANCHOR_DOC in out["meta"]["suggestions"][bad]


@requires_corpus
def test_section过滤命中只返回该段() -> None:
    full = wiki_read(doc_ids=_ANCHOR_DOC)["data"][0]
    target = full["sections"][0]
    out = wiki_read(doc_ids=_ANCHOR_DOC, section=target)
    doc = out["data"][0]
    assert doc["section_matched"] is True
    assert doc["content"].startswith(f"## {target}")
    assert len(doc["content"]) < len(full["content"])
    assert out["meta"]["section_filter"] == target


@requires_corpus
def test_section未命中返回空正文但保留可选段名() -> None:
    """段名写错时不灌全文 —— sections 已列出可选项，Agent 可据此改参数重试。"""
    out = wiki_read(doc_ids=_ANCHOR_DOC, section="根本不存在的段名")
    doc = out["data"][0]
    assert doc["section_matched"] is False
    assert doc["content"] == ""
    assert doc["sections"], "仍要给出可选段名"


@requires_corpus
def test_批量读并对超出上限的部分截断() -> None:
    ids = ",".join([_ANCHOR_DOC, "sop-f1-cascade-timeout", "sop-f6-dns-lookup-failed", "README"])
    out = wiki_read(doc_ids=ids)
    assert len(out["meta"]["requested"]) == _WIKI_READ_MAX_DOCS
    assert out["meta"]["dropped"] == ["README"]
    assert out["meta"]["found"] <= _WIKI_READ_MAX_DOCS


@requires_corpus
def test_重复ID去重保序() -> None:
    out = wiki_read(doc_ids=f"{_ANCHOR_DOC},{_ANCHOR_DOC}")
    assert out["meta"]["requested"] == [_ANCHOR_DOC]
    assert out["meta"]["found"] == 1


@requires_corpus
def test_可读目录索引README并覆盖全部SOP() -> None:
    """README 是全库目录页 —— 读它等于拿到「故障域 → 文档」映射。

    断言含 F11-F13：目录漏登记新故障类型时，Agent 走目录路由就看不见它们，
    这是 wiki_index 用途的静默失效，必须被测试抓住而不是靠人眼发现。
    """
    out = wiki_read(doc_ids="README")
    assert out["meta"]["found"] == 1
    doc = out["data"][0]
    assert doc["category"] == "index"
    dsts = {lk["doc_id"] for lk in doc["links"]}
    assert len(dsts) > 20, "目录页应链向全库"
    for required in (
        "sop-f11-requeue-storm",
        "sop-f12-stuck-task",
        "sop-f13-401-mass-spread",
    ):
        assert required in dsts, f"目录页漏登记 {required}"


# ---------- 经 TOOL_REGISTRY 的治理层（截断上限）----------
# 上面的测试都直接调裸函数，绕过了 base.Tool 的校验/超时/截断治理。
# 截断恰恰只在治理层发生，所以必须单独覆盖 —— 否则"精读全文"可能是个假承诺。


def _longest_doc() -> tuple[str, int]:
    """全库最长文档的 (doc_id, 字符数)。"""
    paths = list(Path(settings.docs_dir).rglob("*.md"))
    longest = max(paths, key=lambda p: len(p.read_text(encoding="utf-8")))
    return longest.stem, len(longest.read_text(encoding="utf-8"))


@requires_corpus
def test_注册层上限足以覆盖全库() -> None:
    """配置锁：wiki_read 的截断上限必须 ≥ 全库最长文档，且不占检索池。

    写成动态断言而非魔数：文档增长到超过上限时这里会先红，而不是等 Agent 在线上
    悄悄拿到残篇。
    """
    from opsagent.core.tools import TOOL_REGISTRY

    tool = TOOL_REGISTRY["wiki_read"]
    _, longest_len = _longest_doc()
    assert tool.max_str_chars >= longest_len, (
        f"max_str_chars={tool.max_str_chars} < 最长文档 {longest_len} 字符，全文会被截"
    )
    assert tool.max_list_items >= 38, "目录页出链数超过上限会静默丢文档"
    assert tool.heavy is False, "纯文件读不该占用检索专用池"


@requires_corpus
def test_注册层读最长文档正文不被截断() -> None:
    """经治理层读全库最长文档，正文长度应与源文件完全一致。"""
    from opsagent.core.tools import TOOL_REGISTRY

    doc_id, raw_len = _longest_doc()
    out = TOOL_REGISTRY["wiki_read"](doc_ids=doc_id)
    assert out["meta"]["found"] == 1
    assert not out["meta"].get("truncated"), "最长文档被截断，上限过低"
    assert len(out["data"][0]["content"]) == raw_len, "正文长度与源文件不一致"


@requires_corpus
def test_注册层读目录页出链不被截断() -> None:
    """目录页出链数超过默认 max_list_items=30，放宽后每一项都应完整保留。

    截断会在列表尾部塞一条字符串提示（"…还有 N 条已省略"），所以「每项都是 dict」
    就是「未被截断」的判据。
    """
    from opsagent.core.tools import TOOL_REGISTRY

    out = TOOL_REGISTRY["wiki_read"](doc_ids="README")
    links = out["data"][0]["links"]
    assert all(isinstance(lk, dict) for lk in links), "出链列表被截断"
    assert len(links) > 30, "目录页出链应超过默认上限，否则这个测试没意义"
    assert not out["meta"].get("truncated")


@requires_corpus
def test_注册层入参校验拒绝缺失必填() -> None:
    """doc_ids 是必填 —— 治理层应在调用前拦下，而不是让裸函数抛 TypeError。"""
    from opsagent.core.tools import TOOL_REGISTRY
    from opsagent.core.tools.base import ToolValidationError

    with pytest.raises(ToolValidationError):
        TOOL_REGISTRY["wiki_read"](section="相关文档")


@requires_corpus
def test_传路径形式也能读到() -> None:
    out = wiki_read(doc_ids=f"sops/{_ANCHOR_DOC}.md")
    assert out["meta"]["found"] == 1
    assert out["data"][0]["doc_id"] == _ANCHOR_DOC


@requires_corpus
def test_空入参安全返回() -> None:
    out = wiki_read(doc_ids="  ,  ")
    assert out["data"] == []
    assert out["meta"]["requested"] == []
    assert out["meta"]["found"] == 0
