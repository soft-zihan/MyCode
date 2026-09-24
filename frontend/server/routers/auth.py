"""鉴权状态端点：前端据此决定是否要求输入 token（不泄漏 token 本身）。"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(tags=["auth"])


@router.get("/api/auth/status")
async def api_auth_status() -> dict[str, bool]:
    from frontend.server.auth import auth_enabled

    return {"auth_required": auth_enabled()}
