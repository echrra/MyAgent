"""实验指纹 —— 每轮评测自动采集可复现性元数据，让任意两轮可 diff 归因。

为什么要这个模块：
之前分析 Run1 vs Run2 时，vendor 切了、timeout 从 300 调到 330、模型没变——这三个
变量混杂在一起，靠人脑记忆和文档考古才理清。实验指纹把「这一轮在什么条件下跑
的」结构化落盘，两轮之间改了什么，`diff` 两个 JSON 就知道，不靠回忆。

采集内容（全部只读，零副作用）：
  - Git：当前 commit SHA + 是否有未提交改动（dirty）
  - 代码：opsagent/ 与 eval/ 的 .py 文件组合 hash
  - 数据：评测集 YAML、知识库 docs、合成日志 三个目录的内容 hash
  - 模型：graph_version、各节点模型别名、embedding/rerank 模型名
  - 嵌入索引：PG 表名 + BM25 索引目录 hash
  - 框架：langgraph / pydantic / python 版本
  - 运行参数：concurrency、timeout、case 数、case ID 列表 hash

用法：
  from eval.fingerprint import collect_fingerprint, save_fingerprint
  fp = collect_fingerprint(concurrency=3, timeout=330.0, case_ids=[...])
  save_fingerprint(fp, Path("eval/reports"))   # 指纹文件 + JSON 各一份
"""

from __future__ import annotations

import hashlib
import json
import platform
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from importlib.metadata import version as pkg_version
from pathlib import Path
from typing import Any

from opsagent.core.config import PROJECT_ROOT, settings

# ====================== 数据结构 ======================


@dataclass
class ExperimentFingerprint:
    """一轮评测的完整环境指纹。所有字段只读采集，不写不改。"""

    # 元信息
    collected_at: str = ""           # ISO8601 采集时间
    fingerprint_version: int = 1     # 指纹结构版本（结构变更时递增）

    # Git 状态
    git_sha: str = ""                # 当前 commit 短 SHA
    git_dirty: bool = False          # 是否有未提交改动
    git_branch: str = ""             # 当前分支名

    # 代码 hash（.py 文件内容 SHA256 拼接后再 hash，顺序无关）
    code_hash_opsagent: str = ""     # opsagent/ 下所有 .py
    code_hash_eval: str = ""         # eval/ 下所有 .py

    # 数据资产 hash
    dataset_hash: str = ""           # eval/cases/*.yaml
    kb_docs_hash: str = ""           # data/docs/ 知识库原文
    logs_hash: str = ""              # data/logs/synth/ 合成日志

    # 索引标识（不读 DB / BM25 文件内容，只标识配置和路径）
    kb_table: str = ""               # PG chunk 表名
    bm25_index_dir: str = ""         # BM25 索引目录路径

    # 模型配置
    graph_version: str = ""          # v1 / v2
    model_coordinator: str = ""      # 协调器模型别名
    model_worker: str = ""           # Worker 模型别名
    model_synthesizer: str = ""      # 综合器模型别名
    embedding_model: str = ""        # 向量模型名
    rerank_model: str = ""           # 精排模型名

    # 检索参数（影响召回结果的旋钮）
    retrieval_recall_top_n: int = 0
    retrieval_rrf_k: int = 0
    retrieval_min_rerank_score: float = 0.0
    retrieval_rewrite_enabled: bool = False

    # 框架版本
    python_version: str = ""
    langgraph_version: str = ""
    pydantic_version: str = ""
    platform_info: str = ""          # OS + 架构

    # 运行参数（由调用方传入）
    concurrency: int = 0
    timeout_s: float = 0.0
    n_cases: int = 0
    case_ids_hash: str = ""          # case ID 列表 hash（不传完整列表以省体积）


# ====================== 内部工具 ======================


def _sha256_text(text: str) -> str:
    """对一段文本算 SHA256，取前 16 位（够做指纹核验，且便于肉眼比对）。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _dir_content_hash(directory: Path, glob_pattern: str) -> str:
    """目录内容指纹：对匹配文件的「相对路径 + 文件内容」整体 hash。

    设计：不取文件 mtime（会被 touch / git checkout 干扰），
    直接对内容 hash —— 内容没变则指纹不变，内容变了指纹必变。
    """
    if not directory.is_dir():
        return "missing"

    hasher = hashlib.sha256()
    files = sorted(directory.rglob(glob_pattern))
    if not files:
        return "empty"

    for f in files:
        rel = f.relative_to(directory).as_posix()
        hasher.update(rel.encode("utf-8"))
        try:
            hasher.update(f.read_bytes())
        except OSError:
            hasher.update(b"<unreadable>")

    return hasher.hexdigest()[:16]


def _py_files_hash(directory: Path) -> str:
    """Python 源码目录指纹。"""
    return _dir_content_hash(directory, "**/*.py")


def _git_info() -> tuple[str, bool, str]:
    """读取 Git SHA / dirty / branch。Git 不可用时返回空串。"""
    try:
        sha = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5, cwd=PROJECT_ROOT,
        ).stdout.strip()

        # status --porcelain 非空即有未提交改动（含 untracked）
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True, text=True, timeout=5, cwd=PROJECT_ROOT,
        ).stdout.strip()

        branch = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, timeout=5, cwd=PROJECT_ROOT,
        ).stdout.strip()

        return sha, bool(status), branch
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return "", False, ""


def _safe_pkg_version(name: str) -> str:
    """取 pip 包版本；未安装时返回空串。"""
    try:
        return pkg_version(name)
    except Exception:
        return ""


# ====================== 公开接口 ======================


def collect_fingerprint(
    *,
    concurrency: int = 0,
    timeout_s: float = 0.0,
    case_ids: list[str] | None = None,
) -> ExperimentFingerprint:
    """采集当前环境完整指纹。

    Args:
        concurrency: 本轮评测并发数
        timeout_s: 本轮单 case 超时秒数
        case_ids: 本轮评测的 case ID 列表（用于算 hash，不存完整列表）

    Returns:
        填充完毕的 ExperimentFingerprint
    """
    git_sha, git_dirty, git_branch = _git_info()

    # 数据集 hash：对 cases 目录所有 YAML 内容
    # CASES_DIR 在 eval/dataset/__init__.py 中定义，指向 eval/dataset/cases/
    from eval.dataset import CASES_DIR
    dataset_hash = _dir_content_hash(Path(CASES_DIR), "*.yaml")

    # 知识库：所有 .md 文件
    kb_dir = Path(settings.docs_dir)
    kb_hash = _dir_content_hash(kb_dir, "**/*.md")

    # 合成日志：所有 .jsonl 文件
    logs_dir = Path(settings.logs_dir)
    logs_hash = _dir_content_hash(logs_dir, "**/*.jsonl")

    # case ID 列表 hash
    ids_hash = _sha256_text(",".join(sorted(case_ids))) if case_ids else ""

    return ExperimentFingerprint(
        collected_at=datetime.now(timezone.utc).isoformat(),
        git_sha=git_sha,
        git_dirty=git_dirty,
        git_branch=git_branch,
        code_hash_opsagent=_py_files_hash(PROJECT_ROOT / "opsagent"),
        code_hash_eval=_py_files_hash(PROJECT_ROOT / "eval"),
        dataset_hash=dataset_hash,
        kb_docs_hash=kb_hash,
        logs_hash=logs_hash,
        kb_table=settings.kb_table,
        bm25_index_dir=settings.bm25_index_dir,
        graph_version=settings.graph_version,
        model_coordinator=settings.model_coordinator,
        model_worker=settings.model_worker,
        model_synthesizer=settings.model_synthesizer,
        embedding_model=settings.embedding_model,
        rerank_model=settings.rerank_model,
        retrieval_recall_top_n=settings.retrieval_recall_top_n,
        retrieval_rrf_k=settings.retrieval_rrf_k,
        retrieval_min_rerank_score=settings.retrieval_min_rerank_score,
        retrieval_rewrite_enabled=settings.retrieval_rewrite_enabled,
        python_version=platform.python_version(),
        langgraph_version=_safe_pkg_version("langgraph"),
        pydantic_version=_safe_pkg_version("pydantic"),
        platform_info=f"{platform.system()} {platform.machine()}",
        concurrency=concurrency,
        timeout_s=timeout_s,
        n_cases=len(case_ids) if case_ids else 0,
        case_ids_hash=ids_hash,
    )


def save_fingerprint(
    fp: ExperimentFingerprint,
    output_dir: Path,
    run_id: str = "",
) -> Path:
    """指纹落盘为 JSON，文件名含 run_id 便于与评测报告关联。

    Returns:
        写入的 JSON 文件路径
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    name = f"fingerprint_{run_id}.json" if run_id else "fingerprint.json"
    path = output_dir / name
    path.write_text(
        json.dumps(asdict(fp), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path


def diff_fingerprints(
    fp_a: ExperimentFingerprint | dict[str, Any],
    fp_b: ExperimentFingerprint | dict[str, Any],
) -> list[tuple[str, Any, Any]]:
    """对比两个指纹，返回有差异的字段列表。

    Args:
        fp_a: 基线指纹（ExperimentFingerprint 或已从 JSON 加载的 dict）
        fp_b: 对比指纹

    Returns:
        [(字段名, 值A, 值B), ...] 按字段名排序；无差异返回空列表
    """
    dict_a = asdict(fp_a) if isinstance(fp_a, ExperimentFingerprint) else fp_a
    dict_b = asdict(fp_b) if isinstance(fp_b, ExperimentFingerprint) else fp_b

    diffs: list[tuple[str, Any, Any]] = []
    all_keys = sorted(set(dict_a.keys()) | set(dict_b.keys()))

    for key in all_keys:
        val_a = dict_a.get(key)
        val_b = dict_b.get(key)
        if val_a != val_b:
            diffs.append((key, val_a, val_b))

    return diffs


def format_fingerprint_summary(fp: ExperimentFingerprint) -> str:
    """生成人类可读的指纹摘要（嵌入 Markdown 报告头部）。"""
    dirty_mark = " ⚠️dirty" if fp.git_dirty else ""
    return (
        f"**指纹**: git={fp.git_sha}@{fp.git_branch}{dirty_mark} | "
        f"模型={fp.model_coordinator}/{fp.model_worker}/{fp.model_synthesizer} | "
        f"数据集={fp.dataset_hash} | KB={fp.kb_docs_hash} | "
        f"并发={fp.concurrency} 超时={fp.timeout_s}s | "
        f"v={fp.fingerprint_version}"
    )
