"""SSE 端到端冒烟：连 /chat 端点，断言收到 thinking/tool/token/done 事件。

跑法（需先起后端，或用 make test-sse 自动编排）：
    uv run uvicorn opsagent.app.main:app --port 8000 &
    uv run python scripts/test_sse.py

验证点：
- 能连上 /chat 并收到 SSE 事件流
- 至少 1 个 thinking、1 个 tool、若干 token、1 个 done
- 末尾 done 的 final_len > 0
"""

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx

from opsagent.core.config import settings

PROBE_QUERY = "edgectl-backend-watcher 最近 DNS 解析失败了吗？如果有，根据 SOP 给出排查建议。"


async def _parse_sse(resp: httpx.Response):
    """逐行解析 SSE，产出 (event, data) 元组。"""
    event = "message"
    async for line in resp.aiter_lines():
        if not line:  # 空行 = 一条事件结束
            event = "message"
            continue
        if line.startswith("event:"):
            event = line[len("event:"):].strip()
        elif line.startswith("data:"):
            data = line[len("data:"):].strip()
            yield event, data


async def main() -> int:
    base = settings.effective_api_base_url
    url = f"{base}/chat"
    print(f"OpsAgent — SSE 端到端冒烟\n连接: {url}\nQuery: {PROBE_QUERY}\n" + "-" * 60)

    counts = {"thinking": 0, "tool": 0, "token": 0, "done": 0, "error": 0}
    done_payload = None
    answer_buf = []

    timeout = httpx.Timeout(None, connect=10.0)
    async with (
        httpx.AsyncClient(timeout=timeout) as client,
        client.stream("POST", url, json={"query": PROBE_QUERY}) as resp,
    ):
        if resp.status_code != 200:
            print(f"❌ HTTP {resp.status_code}")
            return 1
        async for event, data in _parse_sse(resp):
            if event in counts:
                counts[event] += 1
            if event == "thinking":
                d = json.loads(data)
                print(f"  🤔 [{d.get('iteration')}] {d.get('thought')} → {d.get('next_action')}")
            elif event == "tool":
                d = json.loads(data)
                print(f"  🔧 {d['tool_name']}({d['args']}) → {d['summary']} ({d['latency_ms']}ms)")
            elif event == "token":
                answer_buf.append(json.loads(data).get("t", ""))
            elif event == "done":
                done_payload = json.loads(data)
            elif event == "error":
                print(f"  ❌ error: {json.loads(data).get('message')}")

    print("-" * 60)
    print("💬 答案:\n   " + "".join(answer_buf).replace("\n", "\n   "))
    print(f"\n📊 事件统计: {counts}")
    if done_payload:
        print(f"   done: {done_payload}")

    # 验收
    checks = {
        "收到 thinking": counts["thinking"] >= 1,
        "收到 tool": counts["tool"] >= 1,
        "收到 token": counts["token"] >= 1,
        "收到 done": counts["done"] == 1,
        "无 error": counts["error"] == 0,
        "答案非空": len("".join(answer_buf).strip()) > 0,
    }
    print("\n✅ 验收")
    all_pass = True
    for desc, ok in checks.items():
        print(f"   {'✓' if ok else '✗'} {desc}")
        all_pass = all_pass and ok

    if all_pass:
        print("\n🎉 SSE 端到端通过。")
        return 0
    print("\n⚠️ 部分验收失败")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
