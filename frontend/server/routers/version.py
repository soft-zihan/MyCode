"""Version API — 返回当前应用版本号。"""

from __future__ import annotations

from fastapi import APIRouter

from agents.version import __version__

router = APIRouter(tags=["version"])


@router.get("/api/version")
def api_version() -> dict[str, str]:
    return {"version": __version__}