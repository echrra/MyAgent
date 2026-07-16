"""LLM 烟测脚本：验证 .env 中两家 API Key 是否可用。

跑法:
    make test-llm
    # 或
    uv run python scripts/test_llm.py

成功标准: 4 条别名 (plan / reflect / answer / updater) 全部能正常返回。
失败时根据错误信息定位是 Key 错 / 网络问题 / 余额不足。
"""

import asyncio
import sys
import time
from pathlib import Path

# 将项目根加入 sys.path，方便直接 python 跑
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from opsagent.core.config import settings  # noqa: E402
from opsagent.core.llm.client import chat  # noqa: E402


ALIASES = ["plan", "reflect", "answer", "updater"]
PROBE_QUESTION = "用一句话回答：1+1 等于多少？"


def _check_keys() -> None:
    """先检查 .env 是否填了 Key，否则提前拒绝运行。"""
    missing = []
    if not settings.deepseek_api_key or settings.deepseek_api_key.startswith("sk-your"):
        missing.append("DEEPSEEK_API_KEY")
    if not settings.dashscope_api_key or settings.dashscope_api_key.startswith("sk-your"):
        missing.append("DASHSCOPE_API_KEY")
    if missing:
        print("❌ 缺少 API Key（请编辑 .env）：", ", ".join(missing))
        sys.exit(2)


async def _probe(alias: str) -> tuple[bool, str, float]:
    """对单个别名跑一次最简调用。

    Returns:
        (成功？, 内容或错误信息, 耗时秒)
    """
    started = time.perf_counter()
    try:
        resp = await chat(
            alias=alias,
            messages=[{"role": "user", "content": PROBE_QUESTION}],
            max_tokens=64,
        )
        elapsed = time.perf_counter() - started
        content = resp["choices"][0]["message"]["content"].strip()
        return True, content, elapsed
    except Exception as exc:  # noqa: BLE001
        elapsed = time.perf_counter() - started
        return False, f"{type(exc).__name__}: {exc}", elapsed


async def main() -> int:
    _check_keys()
    print("=" * 60)
    print("OpsAgent — LLM 连通性烟测")
    print("=" * 60)
    print(f"DeepSeek base: {settings.deepseek_base_url}")
    print(f"Qwen     base: {settings.dashscope_base_url}")
    print()

    failures = 0
    for alias in ALIASES:
        ok, content, elapsed = await _probe(alias)
        tag = "✅" if ok else "❌"
        print(f"{tag} [{alias:8s}] ({elapsed:5.2f}s) → {content[:80]}")
        if not ok:
            failures += 1

    print()
    if failures == 0:
        print("🎉 全部别名通过，模型层 OK。")
        return 0
    else:
        print(f"⚠️ {failures} 个别名失败，请检查 .env / 余额 / 网络")
        return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
