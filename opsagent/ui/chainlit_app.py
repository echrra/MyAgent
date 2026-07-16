"""Chainlit 前端 —— 作为后端 SSE 端点的消费者。

职责（不直接跑 Agent，只消费 /chat 的 SSE）：
- thinking 事件    → cl.Step 展示每轮决策 / Coordinator 假设列表
- tool 事件        → cl.Step 展示工具调用与结果摘要
- hypothesis 事件  → cl.Step 展示 Worker 诊断结论（v2 Multi-Agent）
- token 事件       → cl.Message 流式逐字输出答案
- done/error       → 收尾 / 报错

跑法：
    make demo     # 同时起后端 API + 本前端
    # 或单独：uv run chainlit run opsagent/ui/chainlit_app.py
"""

import json
import sys
from pathlib import Path

# Chainlit 用 spec loader 直接加载本文件，不会把项目根加进 sys.path，
# 故手动补上，保证 `import opsagent.*` 可用（与 scripts/test_*.py 同款处理）。
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import chainlit as cl
import httpx

from opsagent.core.config import settings


def _api_url() -> str:
    return f"{settings.effective_api_base_url}/chat"


async def _iter_sse(resp: httpx.Response):
    """逐行解析 SSE，产出 (event, data_str)。"""
    event = "message"
    async for line in resp.aiter_lines():
        if not line:
            event = "message"
            continue
        if line.startswith("event:"):
            event = line[len("event:"):].strip()
        elif line.startswith("data:"):
            yield event, line[len("data:"):].strip()


@cl.on_chat_start
async def on_start():
    await cl.Message(
        content="👋 我是 OpsAgent 运维助手。可问某服务的故障，例如："
        "「edgectl-backend-watcher 最近 DNS 解析失败了吗？」"
    ).send()


@cl.on_message
async def on_message(message: cl.Message):
    # 答案消息：先建空消息，后续 stream_token 逐字填充
    answer = cl.Message(content="")
    await answer.send()

    # SSE 长连接：连接超时 10s，读取不超时（流可能跑很久）
    timeout = httpx.Timeout(None, connect=10.0)
    payload = {"query": message.content}

    try:
        async with (
            httpx.AsyncClient(timeout=timeout) as client,
            client.stream("POST", _api_url(), json=payload) as resp,
        ):
            if resp.status_code != 200:
                await answer.stream_token(f"❌ 后端返回 HTTP {resp.status_code}")
                await answer.update()
                return

            async for event, data in _iter_sse(resp):
                if event == "thinking":
                    d = json.loads(data)
                    # v2 Coordinator 推假设列表
                    if d.get("hypotheses"):
                        hyps = d["hypotheses"]
                        hyp_text = "\n".join(f"  • {h}" for h in hyps)
                        async with cl.Step(name=f"Coordinator: {len(hyps)} 个假设", type="llm") as step:
                            step.output = f"{d.get('thought', '')}\n{hyp_text}"
                    else:
                        # v1 Plan 决策
                        async with cl.Step(name=f"决策 #{d.get('iteration', 0)}", type="llm") as step:
                            step.output = f"{d.get('thought', '')}\n→ 下一步：{d.get('next_action')}"
                elif event == "tool":
                    d = json.loads(data)
                    flag = "✓" if d.get("success") else "✗"
                    async with cl.Step(name=f"工具 {flag} {d['tool_name']}", type="tool") as step:
                        step.input = json.dumps(d.get("args", {}), ensure_ascii=False)
                        step.output = f"{d.get('summary', '')}（{d.get('latency_ms', 0)}ms）"
                elif event == "hypothesis":
                    d = json.loads(data)
                    conf = d.get("confidence", 0)
                    h_id = d.get("hypothesis_id", "?")
                    async with cl.Step(name=f"假设 {h_id} (置信度 {conf:.0%})", type="llm") as step:
                        step.output = f"**{d.get('hypothesis', '')}**\n{d.get('conclusion', '')}"
                elif event == "token":
                    await answer.stream_token(json.loads(data).get("t", ""))
                elif event == "error":
                    msg = json.loads(data).get("message", "未知错误")
                    await answer.stream_token(f"\n\n❌ 执行出错：{msg}")
                elif event == "done":
                    break
    except httpx.ConnectError:
        await answer.stream_token(
            f"❌ 无法连接后端 {_api_url()}，请先用 `make api` 启动后端。"
        )

    await answer.update()
