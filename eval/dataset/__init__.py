"""评测集加载工具 —— 从 YAML 读取 cases，支持 filter/subset。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

CASES_DIR = Path(__file__).parent / "cases"


def load_cases(
    directory: Path | str = CASES_DIR,
    *,
    difficulty: str | None = None,
    tags: list[str] | None = None,
    ids: list[str] | None = None,
) -> list[dict[str, Any]]:
    """加载评测 case，支持按 difficulty/tags/ids 过滤。

    Args:
        directory: YAML 文件所在目录
        difficulty: 仅保留该难度（easy/medium/hard）
        tags: 仅保留含指定 tag 的 case（任一 tag 命中即保留）
        ids: 仅保留指定 ID 的 case

    Returns:
        按 id 排序的 case 列表
    """
    directory = Path(directory)
    cases: list[dict[str, Any]] = []

    for f in sorted(directory.glob("*.yaml")):
        with f.open("r", encoding="utf-8") as fh:
            case = yaml.safe_load(fh)
        if case is None:
            continue
        cases.append(case)

    # 过滤
    if ids is not None:
        id_set = set(ids)
        cases = [c for c in cases if c.get("id") in id_set]
    if difficulty is not None:
        cases = [c for c in cases if c.get("difficulty") == difficulty]
    if tags is not None:
        tag_set = set(tags)
        cases = [c for c in cases if tag_set & set(c.get("tags", []))]

    return cases


def load_quick_subset(directory: Path | str = CASES_DIR) -> list[dict[str, Any]]:
    """加载 quick subset（标记 tags=[quick] 的 10 条）用于 PR 级快速回归。"""
    return load_cases(directory, tags=["quick"])
