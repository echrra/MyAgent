"""
LLM Critic —— qwen-turbo 给"已填好"的合成日志打分，决定是否入库。

设计要点：
    1. 评分维度（在 system prompt 里讲明）：
       - 真实性：msg/Content 是否像真实生产日志（无模板感、无 AI 腔）
       - 一致性：msg 与 phrase_hint / level / path / status_code 是否自洽
       - 多样性：同批次内是否过度雷同
    2. 输出严格 JSON：{"score": 0-1, "reason": "..."}
    3. 批量判分：每次最多 BATCH_SIZE 行（避免单条过贵；多了 LLM 注意力差）
    4. 重试策略由调用方（run_all）控制：重生成 ≤ 3 次仍 < 阈值 → 丢弃 + 计数

    注：Critic 与 Generator 同时调用别名解耦，方便后续替换。
"""
from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from typing import Any

from loguru import logger

from opsagent.core.llm.client import chat

# 单次 Critic 请求评估的行数；与 Generator batch_size 保持对齐
DEFAULT_BATCH_SIZE = 8
# 合格阈值（与 docs/09 §2.3 line 73 约定一致）
DEFAULT_THRESHOLD = 0.7


_SYSTEM_PROMPT = """\
你是一个日志真实度审查官。给一批"已合成"的日志，按以下三个维度联合打 1 个 0.0~1.0 的分：

1. 真实性（40%）：msg / Content 像不像真实 GoFrame glog 日志？有无 AI 套话、模板腔、明显占位（如"some error here"）。
2. 一致性（40%）：msg 与给定的 level / path / status_code / phrase_hint 是否自洽？例如 status_code=504 但 msg 写 "request completed" 就算严重不一致。
3. 多样性（20%）：同一批次内的 msg 是否雷同？高度雷同扣分。

输出严格 JSON（不要 markdown 围栏）：
{"scores": [{"score": 0.85, "reason": "<10字内中文>"}, ...]}

scores 数组长度必须等于输入数组长度，顺序一致。
"""


@dataclass
class CriticVerdict:
    """单行评分结果。"""
    score: float
    reason: str


class LLMCritic:
    """批量打分。
    """

    def __init__(
        self,
        alias: str = "critic",
        batch_size: int = DEFAULT_BATCH_SIZE,
        threshold: float = DEFAULT_THRESHOLD,
    ) -> None:
        self.alias = alias
        self.batch_size = batch_size
        self.threshold = threshold

        self.stats = {
            "batches_total": 0,
            "batches_failed": 0,
            "rows_judged": 0,
            "rows_pass": 0,
            "rows_fail": 0,
            "score_sum": 0.0,
        }

    @property
    def avg_score(self) -> float:
        n = self.stats["rows_judged"]
        return self.stats["score_sum"] / n if n else 0.0

    # ------------------------------------------------------------------
    # 对外主入口
    # ------------------------------------------------------------------
    async def judge(self, rows: list[dict[str, Any]]) -> list[CriticVerdict]:
        """给一批"已填充"的 jsonl 行打分。

        Args:
            rows: llm_generator.fill() 的输出（含 msg / Content / level / ...）

        Returns:
            与 rows 同长的 CriticVerdict 列表
        """
        if not rows:
            return []

        batches: list[list[dict[str, Any]]] = []
        for i in range(0, len(rows), self.batch_size):
            batches.append(rows[i : i + self.batch_size])

        coros = [self._judge_one_batch(b) for b in batches]
        batch_results = await asyncio.gather(*coros, return_exceptions=False)

        out: list[CriticVerdict] = []
        for vs in batch_results:
            out.extend(vs)
        return out

    # ------------------------------------------------------------------
    # 私有：单批次评分
    # ------------------------------------------------------------------
    async def _judge_one_batch(self, batch: list[dict[str, Any]]) -> list[CriticVerdict]:
        self.stats["batches_total"] += 1

        # 给 LLM 看的精简视图（带必要上下文）
        prompt_items = [
            {
                "level": r.get("Level"),
                "path": r.get("path"),
                "status_code": r.get("status_code"),
                "msg": r.get("msg"),
                # Content 截 200 字避免过长
                "content": (r.get("Content") or "")[:200] or None,
            }
            for r in batch
        ]

        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(prompt_items, ensure_ascii=False)},
        ]

        try:
            resp = await chat(
                alias=self.alias,
                messages=messages,
                response_format={"type": "json_object"},
                max_tokens=1024,
            )
            text = resp["choices"][0]["message"]["content"]
            verdicts = self._parse(text, expected=len(batch))
        except Exception as exc:
            logger.warning("Critic batch 失败 ({}): {}", type(exc).__name__, exc)
            self.stats["batches_failed"] += 1
            verdicts = None

        if verdicts is None:
            # 评分失败 → 给中性分（0.5），不当作 pass 也不当 fail
            verdicts = [CriticVerdict(score=0.5, reason="critic失败") for _ in batch]

        # 更新统计
        for v in verdicts:
            self.stats["rows_judged"] += 1
            self.stats["score_sum"] += v.score
            if v.score >= self.threshold:
                self.stats["rows_pass"] += 1
            else:
                self.stats["rows_fail"] += 1

        return verdicts

    # ------------------------------------------------------------------
    # 私有：响应解析
    # ------------------------------------------------------------------
    @staticmethod
    def _parse(text: str, expected: int) -> list[CriticVerdict] | None:
        cleaned = text.strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
            cleaned = re.sub(r"\s*```$", "", cleaned)
        try:
            obj = json.loads(cleaned)
        except json.JSONDecodeError:
            return None
        scores = obj.get("scores")
        if not isinstance(scores, list) or len(scores) != expected:
            return None

        out: list[CriticVerdict] = []
        for item in scores:
            try:
                score = float(item.get("score", 0.0))
            except (TypeError, ValueError):
                score = 0.0
            # 钳到 [0, 1]
            score = max(0.0, min(1.0, score))
            reason = str(item.get("reason", ""))[:30]
            out.append(CriticVerdict(score=score, reason=reason))
        return out
