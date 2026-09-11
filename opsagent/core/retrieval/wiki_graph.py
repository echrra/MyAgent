"""知识库链接图 —— 把手写的 md 交叉引用解析成可遍历的有向图。

为什么要这层：
知识库各篇文档末尾的「相关文档 / 关联文档」段里有约 40 条手写交叉引用，外加
README.md 一张覆盖全库的目录表。但检索链把文档切成 chunk 后只保留正文向量，
链接结构全部丢失 —— agent 检索到 pm-008 时看得见「→ SOP-F8」这行文字，却没有
任何工具能顺着跳过去。本模块把这些链接还原成图，供 wiki_read / wiki_index 使用。

设计要点：
- README.md 是目录页（出链覆盖全库）。若与内容链接放同一张图，任意两篇文档距离
  都会 ≤2，遍历彻底失去区分度 —— 故标为 category="index"，遍历默认排除，只作目录用。
- 断链只标记不抛错：知识库是手写的，新增文档时漏建目标文件是高频错误，
  wiki_read 遇到断链要能优雅提示而非报错。
- 纯 stdlib 实现，不依赖 DB / 向量模型 / LLM，可完全离线运行与单测。
"""

from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from loguru import logger

from opsagent.core.config import settings

# markdown 行内链接：[锚文本](目标.md) —— 只收 .md，忽略图片/外链/锚点跳转
_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)\s]+?\.md)(?:#[^)]*)?\)")
# 目录页文件名（出链覆盖全库，不参与内容遍历）
_INDEX_FILENAME = "readme.md"
# 目录页的 category 标记
INDEX_CATEGORY = "index"


@dataclass(frozen=True)
class WikiLink:
    """一条文档间链接。"""

    src: str        # 源 doc_id
    dst: str        # 目标 doc_id（相对路径已解析并归一为 stem）
    anchor: str     # 链接锚文本，供 agent 判断这一跳值不值得走
    line: int       # 源文件行号（1 基），可溯源
    dangling: bool  # 目标文档是否不存在


@dataclass(frozen=True)
class DocMeta:
    """一篇文档的元数据（不含正文，正文按需读）。"""

    doc_id: str      # 文件名去扩展，如 sop-f8-configsdk-disconnect
    title: str       # 全篇 H1 标题，缺失则回退 doc_id
    category: str    # 父目录名：sops / postmortems / runbooks / services，目录页为 index
    rel_path: str    # 相对 docs_dir 的路径，如 sops/sop-f8-configsdk-disconnect.md

    @property
    def is_index(self) -> bool:
        """是否为目录页（README）。"""
        return self.category == INDEX_CATEGORY


@dataclass
class WikiGraph:
    """知识库有向图。out_links / in_links 的 key 覆盖全部已存在文档。"""

    docs: dict[str, DocMeta] = field(default_factory=dict)
    out_links: dict[str, list[WikiLink]] = field(default_factory=dict)
    in_links: dict[str, list[WikiLink]] = field(default_factory=dict)

    # ---------- 查询 ----------

    def has(self, doc_id: str) -> bool:
        """文档是否存在。"""
        return doc_id in self.docs

    def out_degree(self, doc_id: str, *, include_dangling: bool = False) -> int:
        """出度。默认只数指向真实存在文档的链接。"""
        links = self.out_links.get(doc_id, ())
        return sum(1 for lk in links if include_dangling or not lk.dangling)

    def in_degree(self, doc_id: str, *, include_index: bool = False) -> int:
        """入度。默认排除目录页的链接（README 链全库，计进去没有区分度）。"""
        links = self.in_links.get(doc_id, ())
        if include_index:
            return len(links)
        return sum(1 for lk in links if not self._is_index(lk.src))

    def dangling(self) -> list[WikiLink]:
        """全库断链清单（目标文档不存在），按 src 排序便于稳定输出。"""
        out = [lk for links in self.out_links.values() for lk in links if lk.dangling]
        return sorted(out, key=lambda lk: (lk.src, lk.line))

    def neighbors(
        self,
        doc_id: str,
        *,
        depth: int = 1,
        include_index: bool = False,
    ) -> dict[str, int]:
        """从 doc_id 出发 BFS，返回 {可达 doc_id: 跳数}，不含自身与断链目标。

        Args:
            doc_id: 起点文档
            depth: 最大跳数（1 = 只看直接相邻）
            include_index: 是否允许经由目录页中转。默认 False —— README 链全库，
                放开会让任意两点距离 ≤2，遍历失去区分度。

        Returns:
            {doc_id: 跳数}；起点不存在或 depth < 1 时返回空 dict。
        """
        if depth < 1 or doc_id not in self.docs:
            return {}

        seen: dict[str, int] = {}
        queue: deque[tuple[str, int]] = deque([(doc_id, 0)])
        while queue:
            cur, dist = queue.popleft()
            if dist >= depth:
                continue
            for lk in self.out_links.get(cur, ()):
                # 断链无法走到；目标必须真实存在
                if lk.dangling or lk.dst not in self.docs:
                    continue
                # 默认不经由目录页中转（但目录页本身可以作为终点被记录）
                if not include_index and self._is_index(lk.dst):
                    continue
                if lk.dst == doc_id or lk.dst in seen:
                    continue
                seen[lk.dst] = dist + 1
                queue.append((lk.dst, dist + 1))
        return seen

    def reverse_reachable(self, *, depth: int = 1) -> dict[str, list[str]]:
        """反向可达集：{目标 doc_id: [能在 depth 跳内到达它的内容层来源...]}。

        对每篇内容文档跑一次 BFS 再反转，避免「每个目标 × 每篇文档」重复遍历。
        目录页不作为起点（它链全库，作为起点会让所有文档看起来都可达）。

        覆盖率类判定必须用本方法而非 in_degree —— in_degree 只算 1 跳直接入链，
        用它当判据会让 depth 参数完全不起作用。

        Returns:
            覆盖全部已存在文档的 dict；无来源可达的目标对应空列表。
        """
        out: dict[str, list[str]] = {d: [] for d in self.docs}
        if depth < 1:
            return out
        for src, meta in self.docs.items():
            if meta.is_index:
                continue
            for dst in self.neighbors(src, depth=depth):
                out[dst].append(src)
        for dst in out:
            out[dst].sort()
        return out

    def reaches(self, srcs: list[str], target: str, *, depth: int = 1) -> tuple[bool, int]:
        """从任一起点出发能否在 depth 跳内到达 target。

        用于回答「检索返回的这批文档，顺链接能不能摸到期望文档」。

        Returns:
            (是否可达, 最短跳数)；不可达时跳数为 -1。target 本身在 srcs 里视为 0 跳。
        """
        if target in srcs:
            return True, 0
        best = -1
        for s in srcs:
            hops = self.neighbors(s, depth=depth).get(target)
            if hops is not None and (best < 0 or hops < best):
                best = hops
        return best >= 0, best

    # ---------- 内部 ----------

    def _is_index(self, doc_id: str) -> bool:
        meta = self.docs.get(doc_id)
        return meta is not None and meta.is_index


def _extract_h1(lines: list[str], fallback: str) -> str:
    """取首个 `# ` 一级标题，缺失则用 fallback（与 chunker 口径一致）。"""
    for ln in lines:
        if ln.lstrip().startswith("# "):
            return ln.lstrip()[2:].strip()
    return fallback


def _resolve_target(src_file: Path, raw_target: str, base: Path) -> str | None:
    """把链接里的相对路径解析成 doc_id（文件名去扩展）。

    支持同目录（`sop-f1-x.md`）、跨目录（`../sops/sop-f1-x.md`）、
    从库根出发（README 里的 `sops/sop-f1-x.md`）三种写法。
    解析结果落在 docs_dir 之外时返回 None（防目录穿越）。
    """
    candidate = (src_file.parent / raw_target).resolve()
    try:
        candidate.relative_to(base.resolve())
    except ValueError:
        logger.warning(f"[wiki_graph] 链接目标越出知识库目录，已忽略: {raw_target}")
        return None
    return candidate.stem


def _scan_docs(base: Path) -> dict[str, DocMeta]:
    """扫全库 md，建立 doc_id → DocMeta（README 标为 index category）。"""
    docs: dict[str, DocMeta] = {}
    for md in sorted(base.rglob("*.md")):
        lines = md.read_text(encoding="utf-8").splitlines()
        doc_id = md.stem
        is_index = md.name.lower() == _INDEX_FILENAME
        category = INDEX_CATEGORY if is_index else md.parent.name
        docs[doc_id] = DocMeta(
            doc_id=doc_id,
            title=_extract_h1(lines, doc_id),
            category=category,
            rel_path=md.relative_to(base).as_posix(),
        )
    return docs


def build_wiki_graph(docs_dir: str | Path | None = None) -> WikiGraph:
    """扫描知识库构建链接图。docs_dir 缺省取 settings.docs_dir。

    知识库目录不存在时返回空图（与 chunker 的降级口径一致，不抛异常）。
    """
    base = Path(docs_dir) if docs_dir is not None else Path(settings.docs_dir)
    if not base.exists():
        logger.warning(f"[wiki_graph] 知识库目录不存在: {base}")
        return WikiGraph()

    docs = _scan_docs(base)
    graph = WikiGraph(
        docs=docs,
        out_links={d: [] for d in docs},
        in_links={d: [] for d in docs},
    )

    for meta in docs.values():
        src_file = base / meta.rel_path
        for lineno, line in enumerate(src_file.read_text(encoding="utf-8").splitlines(), start=1):
            for anchor, raw_target in _LINK_RE.findall(line):
                dst = _resolve_target(src_file, raw_target, base)
                if dst is None or dst == meta.doc_id:  # 越界或自引用，跳过
                    continue
                link = WikiLink(
                    src=meta.doc_id,
                    dst=dst,
                    anchor=anchor.strip(),
                    line=lineno,
                    dangling=dst not in docs,
                )
                graph.out_links[meta.doc_id].append(link)
                if not link.dangling:
                    graph.in_links[dst].append(link)

    n_links = sum(len(v) for v in graph.out_links.values())
    n_dangling = len(graph.dangling())
    logger.info(
        f"[wiki_graph] 建图完成：{len(docs)} 篇文档 / {n_links} 条链接 / {n_dangling} 条断链"
    )
    return graph


@lru_cache(maxsize=1)
def get_wiki_graph() -> WikiGraph:
    """进程级单例（供 wiki_read / wiki_index 工具复用，避免每次调用重扫全库）。"""
    return build_wiki_graph()
