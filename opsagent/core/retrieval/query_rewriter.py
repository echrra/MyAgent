"""Query Rewriting —— 用 LLM 把口语化症状改写为检索友好的关键词。

设计动机（P0 攻坚核心）：
评测发现 92% 失败 case 的 cite=0，根因是用户用口语描述症状（"接口变慢被拖住"），
而知识库 SOP 用的是技术术语（"级联超时"）。bge-large-zh 的双塔 embedding 无法
桥接这个语义鸿沟。

方案：在 retrieve() 前加一步 LLM 改写，把症状翻译为检索关键词——
这是 HyDE（Hypothetical Document Embeddings）的轻量变体，
只改写 query 不生成假设文档，延迟可控（+200-400ms）。

降级策略：改写失败时返回原 query，不影响检索链路。
"""

from __future__ import annotations

from loguru import logger

from opsagent.core.config import settings

# 改写 prompt：极短，引导 LLM 输出检索关键词
_REWRITE_PROMPT = """\
你是一个运维知识库检索助手。把下面的运维问题描述改写为检索关键词。

要求：
- 包含中英文专业术语（如 cascade timeout、熔断、OOM 等）
- 包含可能的故障类型关键词（如 DNS、Kafka、MySQL、panic 等）
- 直接输出关键词，空格分隔，不超过 50 字
- 不要输出解释或前缀

问题：{query}"""


def rewrite_query(query: str) -> str:
    """同步调用 LLM 改写 query 为检索关键词。

    在 tool_exec 的 worker 线程内被调用（asyncio.to_thread），
    此处用 Router.completion（同步）避免嵌套事件循环。
    第一次失败/空串 → 重试一次 → 再失败才降级原 query。
    """
    if not settings.retrieval_rewrite_enabled:
        return query

    max_attempts = 2
    for attempt in range(max_attempts):
        try:
            from opsagent.core.llm.client import get_router

            router = get_router()
            messages = [{"role": "user", "content": _REWRITE_PROMPT.format(query=query)}]

            response = router.completion(
                model="updater",
                messages=messages,
                temperature=0.0,
                max_tokens=80,
            )
            rewritten = response.choices[0].message.content.strip()

            # 基本校验：改写结果非空且不过长
            if rewritten and len(rewritten) < 200:
                combined = f"{query} {rewritten}"
                logger.debug(f"[query_rewrite] '{query}' → '{rewritten}'")
                return combined

            # 空串或异常长度，重试
            if attempt < max_attempts - 1:
                logger.debug(f"[query_rewrite] 第 {attempt+1} 次返回空串，重试")
                continue

            logger.warning(f"[query_rewrite] 改写结果异常，降级原 query: '{rewritten}'")
            return query

        except Exception as exc:
            if attempt < max_attempts - 1:
                logger.debug(f"[query_rewrite] 第 {attempt+1} 次异常，重试: {exc}")
                continue
            logger.warning(f"[query_rewrite] 改写失败，降级原 query: {exc}")
            return query

    return query
