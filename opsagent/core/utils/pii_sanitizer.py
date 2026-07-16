"""PII 正则清洗器（真实日志接入 LLM 前的强制脱敏层）。

背景：R3 验证时发现，F13 真实日志里每条 msg 都带完整 JWT token，payload 段 base64 解码后
是明文的 userId/deptId/deptName 等 PII 字段组合。LLM provider 的内容安全策略识别到"结构化
PII"会静默拒答（synthesizer 返回 0 字），表现极其隐蔽。

规则设计：
    - JWT：保留 header 里的 kid/alg（对诊断有价值），整个 token 替换为 <JWT kid=X alg=Y>
    - 手机号 / 身份证 / 邮箱：占位符替换
    - Basic auth in URL：user:pwd@host → <CRED>@host
    - IPv4：默认保留（服务端 IP 有诊断价值）；如需脱敏另开 flag

设计原则：
    1. 脱敏后要保留"诊断线索"：kid、错误码、taskId 都不能动
    2. 幂等：重复清洗结果不变
    3. 纯函数，无副作用，方便在 adapter 层随手 pipeline 一下
"""

from __future__ import annotations

import base64
import json
import re
from typing import Callable


# JWT 三段结构：eyJ<header>.eyJ<payload>.<signature>
# header/payload 段以 "eyJ" 开头（base64url 编码的 `{"...`）
_RE_JWT = re.compile(r"\beyJ[A-Za-z0-9_\-]+\.eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+")

# 手机号（中国大陆）
_RE_PHONE = re.compile(r"\b1[3-9]\d{9}\b")

# 邮箱
_RE_EMAIL = re.compile(r"\b[\w.\-+]+@[\w.\-]+\.[a-zA-Z]{2,}\b")

# 身份证（18 位；15 位实际已很少见，先不覆盖）
_RE_ID_CARD = re.compile(r"\b\d{17}[\dXx]\b")

# URL 里的 basic auth：scheme://user:password@host
_RE_URL_AUTH = re.compile(r"(https?://)[^:\s/@]+:[^@\s/]+@")


def _redact_jwt(match: re.Match) -> str:
    """把 JWT 替换成 <JWT kid=X alg=Y>，保留 header 里的诊断字段。"""
    token = match.group(0)
    try:
        header_b64 = token.split(".", 1)[0]
        # base64url 补齐 padding
        pad = 4 - (len(header_b64) % 4)
        if pad != 4:
            header_b64 = header_b64 + "=" * pad
        header = json.loads(base64.urlsafe_b64decode(header_b64))
        kid = header.get("kid", "?")
        alg = header.get("alg", "?")
        return f"<JWT kid={kid} alg={alg}>"
    except Exception:
        # 解不出 header 时也不能把原 token 放回去，用兜底占位
        return "<JWT>"


def sanitize(text: str) -> str:
    """对单段文本做全套 PII 清洗。空/None 直接返回。"""
    if not text:
        return text or ""
    out = _RE_JWT.sub(_redact_jwt, text)
    out = _RE_URL_AUTH.sub(r"\1<CRED>@", out)
    out = _RE_ID_CARD.sub("<ID_CARD>", out)
    out = _RE_PHONE.sub("<PHONE>", out)
    out = _RE_EMAIL.sub("<EMAIL>", out)
    return out


def sanitize_dict(row: dict, fields: list[str] | None = None) -> dict:
    """对 row 里指定字段做清洗。默认清洗 msg + Content 两个字段。"""
    fields = fields or ["msg", "Content"]
    for f in fields:
        if f in row and isinstance(row[f], str):
            row[f] = sanitize(row[f])
    return row


def make_pipeline(*fns: Callable[[str], str]) -> Callable[[str], str]:
    """把多个 sanitizer 串成一个函数（预留扩展点，比如按客户加自定义规则）。"""
    def _run(x: str) -> str:
        for fn in fns:
            x = fn(x)
        return x
    return _run
