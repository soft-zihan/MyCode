"""U13 远程访问安全：绑定地址 + 可选 token 鉴权。

设计（对齐 v2 远程方案）：
- 默认绑定 127.0.0.1——默认安全，暴露必须显式配置（MYCODE_HOST）。
- SSH 隧道是远程首选路径（加密免费、无需鉴权）：ssh -L 5555:localhost:5555 server。
- 直接暴露时必须设 MYCODE_AUTH_TOKEN：HTTP 校验 Authorization: Bearer，
  WS 握手无法带 header（v2 用一次性 ticket，我们用 query token——静态 token
  走 query 有访问日志泄漏面，因此文档要求直接暴露仅限受信内网/配合 TLS 反代）。
- 豁免路径仅限：非 /api（前端静态资源）、/api/health（启动探活）、
  /api/auth/status（前端据此决定是否弹 token 输入，不泄漏 token 本身）。
"""

from __future__ import annotations

import hmac
import os

TOKEN_ENV = "MYCODE_AUTH_TOKEN"
HOST_ENV = "MYCODE_HOST"
DEFAULT_HOST = "127.0.0.1"

EXEMPT_PATHS = frozenset({"/api/health", "/api/auth/status"})


def get_bind_host() -> str:
    return os.environ.get(HOST_ENV) or DEFAULT_HOST


def get_auth_token() -> str | None:
    return os.environ.get(TOKEN_ENV) or None


def auth_enabled() -> bool:
    return get_auth_token() is not None


def is_exempt_path(path: str) -> bool:
    """非 /api 路径（静态资源）与显式豁免端点不需要鉴权。"""
    return not path.startswith("/api") or path in EXEMPT_PATHS


def verify_bearer(authorization_header: str | None) -> bool:
    """校验 Authorization: Bearer <token>。未启用鉴权时恒放行。"""
    token = get_auth_token()
    if token is None:
        return True
    if not authorization_header:
        return False
    scheme, _, value = authorization_header.partition(" ")
    if scheme.lower() != "bearer" or not value:
        return False
    return hmac.compare_digest(value, token)


def verify_ws_token(query_token: str | None) -> bool:
    """校验 WS 握手 query token。未启用鉴权时恒放行。"""
    token = get_auth_token()
    if token is None:
        return True
    return query_token is not None and hmac.compare_digest(query_token, token)
