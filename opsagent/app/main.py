"""FastAPI 应用入口 —— OpsAgent 后端 API。

启动：
    uv run uvicorn opsagent.app.main:app --host $APP_HOST --port $APP_PORT
    # 或 make api

提供：
    GET  /healthz  健康检查
    POST /chat     SSE 流式对话（见 routes/chat.py）
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from opsagent.app.routes import chat

app = FastAPI(title="OpsAgent API", version="0.1.0")

# 允许 Chainlit 前端跨域访问（W1 本地开发放开；生产应收敛白名单）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(chat.router, tags=["chat"])


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    """健康检查：探活用。"""
    return {"status": "ok"}
