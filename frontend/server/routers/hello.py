"""Hello API — 简单的问候端点。"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(tags=["hello"])


@router.get("/api/hello")
def api_hello() -> dict[str, str]:
    return {"greeting": "Hello, World!"}