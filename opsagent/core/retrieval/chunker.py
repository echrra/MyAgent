"""知识库切分器 —— 把 md 文档按二级标题（##）切成检索 chunk。

切分策略（见 W2 plan）：
- 以 `## ` 二级标题为最小 chunk 单位；`# ` 一级标题作为全篇 title 贴进每个 chunk 的
  content 前缀，给向量更多上下文。
- 过短段（正文非空行 < _MIN_BODY_LINES）并入上一个 chunk，避免碎片。
- 超长段（字符数 > _MAX_CHARS）按空行分段二次切，保持 section 归属不变。
- 每个 chunk 带全套元数据（doc_id/title/category/section/chunk_index/source_lines），
  rerank 后可溯源到原文行号（呼应"引用可溯源"防幻觉设计）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import yaml
from loguru import logger

from opsagent.core.config import settings

# 段落并入阈值：正文（不含标题行）非空行少于该值，则并入上一个 chunk
_MIN_BODY_LINES = 3
# 单 chunk 字符上限：超过则按空行二次切（防止超长段稀释向量语义）
_MAX_CHARS = 1200
# 口语症状旁路表文件名（与知识库文档同目录），见 data/docs/symptoms.yaml 头注释
_SYMPTOMS_FILE = "symptoms.yaml"


@lru_cache(maxsize=1)
def _load_symptoms() -> dict[str, str]:
    """加载 doc_id → 口语症状摘要 映射（缺失/解析失败则返回空，不影响切分）。

    用途见 chunk 整形：把用户口语症状前置到 chunk，桥接「口语 query ↔ 技术正文」鸿沟，
    把 reranker（cross-encoder）对不对称 pair 的偏低打分拉过阈值，稳住 cite 命中率。
    """
    path = Path(settings.docs_dir) / _SYMPTOMS_FILE
    if not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        logger.warning(f"[chunker] 解析 {_SYMPTOMS_FILE} 失败，跳过 chunk 整形: {exc}")
        return {}
    # 仅保留 value 为非空字符串的项
    return {k: v.strip() for k, v in data.items() if isinstance(v, str) and v.strip()}


@dataclass
class Chunk:
    """一个检索单元（对应 kb_chunks 表一行）。"""

    doc_id: str          # 文件名去扩展，如 sop-f6-dns-lookup-failed
    title: str           # 全篇 H1 标题
    category: str        # 父目录名：sops / postmortems / runbooks / services
    section: str         # 所属 H2 标题
    chunk_index: int     # 该文档内的序号（从 0 起）
    content: str         # 入库/向量化的正文（含 title + section 前缀）
    source_lines: str    # 原文行号范围，如 "12-38"
    # 仅切分期内部用，不入库
    _body_lines: list[str] = field(default_factory=list, repr=False)


def _extract_h1(lines: list[str], fallback: str) -> str:
    """取首个 `# ` 一级标题，缺失则用 fallback（文件名）。"""
    for ln in lines:
        if ln.lstrip().startswith("# "):
            return ln.lstrip()[2:].strip()
    return fallback


def _split_sections(lines: list[str]) -> list[tuple[str, int, int]]:
    """按 `## ` 切出各 section，返回 [(section_title, start_idx, end_idx)]（行号 0 基，左闭右开）。

    H1 与首个 `## ` 之间的前言并入第一个 section；无 `## ` 时整篇作为一个 section。
    """
    heads = [i for i, ln in enumerate(lines) if ln.lstrip().startswith("## ")]
    if not heads:
        return [("正文", 0, len(lines))]

    sections: list[tuple[str, int, int]] = []
    for k, h in enumerate(heads):
        end = heads[k + 1] if k + 1 < len(heads) else len(lines)
        title = lines[h].lstrip()[3:].strip()
        # 第一个 section 把 H1 之后的前言一起纳入（起点从 0 开始）
        start = 0 if k == 0 else h
        sections.append((title, start, end))
    return sections


def _body_nonempty(lines: list[str]) -> int:
    """统计正文非空行数（用于判断是否过短需并入）。"""
    return sum(1 for ln in lines if ln.strip())


def _split_long(text: str) -> list[str]:
    """超长正文按空行分段，逐段累加到不超过 _MAX_CHARS。"""
    paras = [p for p in text.split("\n\n") if p.strip()]
    out: list[str] = []
    buf = ""
    for p in paras:
        if buf and len(buf) + len(p) + 2 > _MAX_CHARS:
            out.append(buf)
            buf = p
        else:
            buf = f"{buf}\n\n{p}" if buf else p
    if buf:
        out.append(buf)
    return out or [text]


def chunk_doc(path: Path) -> list[Chunk]:
    """切分单篇 md。"""
    raw = path.read_text(encoding="utf-8")
    lines = raw.splitlines()
    doc_id = path.stem
    category = path.parent.name
    title = _extract_h1(lines, doc_id)
    # 口语症状摘要：有则前置到本文档每个 chunk，桥接 query↔doc 文体鸿沟（chunk 整形）
    symptom = _load_symptoms().get(doc_id, "")
    symptom_prefix = f"【适用症状】{symptom}\n\n" if symptom else ""

    chunks: list[Chunk] = []
    pending_short: list[str] | None = None  # 待并入的过短段正文

    for section, start, end in _split_sections(lines):
        seg_lines = lines[start:end]
        seg_text = "\n".join(seg_lines).strip()
        if not seg_text:
            continue

        # 过短段：缓存起来并入下一段（若已是最后一段则并入上一 chunk）
        if _body_nonempty(seg_lines) < _MIN_BODY_LINES:
            if chunks:
                chunks[-1].content += f"\n\n{seg_text}"
                chunks[-1].source_lines = _merge_lines(chunks[-1].source_lines, start, end)
            else:
                pending_short = (pending_short or []) + seg_lines
            continue

        prefix_lines = (pending_short or []) + seg_lines
        pending_short = None
        prefix_text = "\n".join(prefix_lines).strip()

        # 超长段二次切；每个子段都带 [title] section 前缀供向量定位
        pieces = _split_long(prefix_text) if len(prefix_text) > _MAX_CHARS else [prefix_text]
        for piece in pieces:
            content = f"{symptom_prefix}[{title}] {section}\n\n{piece}"
            chunks.append(
                Chunk(
                    doc_id=doc_id,
                    title=title,
                    category=category,
                    section=section,
                    chunk_index=len(chunks),
                    content=content,
                    source_lines=f"{start + 1}-{end}",
                )
            )

    # 收尾：仅有过短前言（无任何正常段）时也兜底成一个 chunk
    if pending_short and not chunks:
        text = "\n".join(pending_short).strip()
        chunks.append(
            Chunk(
                doc_id=doc_id, title=title, category=category, section="正文",
                chunk_index=0, content=f"{symptom_prefix}[{title}]\n\n{text}",
                source_lines=f"1-{len(lines)}",
            )
        )
    return chunks


def _merge_lines(existing: str, start: int, end: int) -> str:
    """把新并入段的行号范围并进已有 source_lines（取最小起点~最大终点）。"""
    try:
        lo = int(existing.split("-")[0])
    except (ValueError, IndexError):
        lo = start + 1
    return f"{min(lo, start + 1)}-{end}"


def chunk_all() -> list[Chunk]:
    """切分知识库全部 md（跳过 README.md）。"""
    base = Path(settings.docs_dir)
    if not base.exists():
        logger.warning(f"[chunker] 知识库目录不存在: {base}")
        return []

    chunks: list[Chunk] = []
    for md in sorted(base.rglob("*.md")):
        if md.name.lower() == "readme.md":
            continue
        chunks.extend(chunk_doc(md))

    logger.info(f"[chunker] 切分完成：{len(chunks)} 个 chunk")
    return chunks
