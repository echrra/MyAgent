"""Prompt 模板加载器。

约定:
- Prompt 文本统一放 .txt（不放 .py，避免转义和 IDE 报错）
- 通过 `load(name)` 读取，纯文本无渲染（变量用 str.format 在节点里自己渲染）
- 缓存到内存，避免每次 IO
"""

from functools import lru_cache
from pathlib import Path

_PROMPT_DIR = Path(__file__).parent


@lru_cache(maxsize=32)
def load(name: str) -> str:
    """按文件名（不含扩展）加载 prompt 文本。"""
    path = _PROMPT_DIR / f"{name}.txt"
    if not path.exists():
        raise FileNotFoundError(f"Prompt 文件不存在: {path}")
    return path.read_text(encoding="utf-8").strip()
