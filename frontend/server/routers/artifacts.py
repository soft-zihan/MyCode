"""U9 artifacts 只读通道：伺服会话图表产物（visualization skill 输出）。

目录约定：<workspace>/.mycode/artifacts/<session_id>/<filename>。
安全纪律：
- workspace 从 SessionManager.get_or_load 解析（BC-26：活会话内存优先，不触发
  死会话修复写）；
- session_id/filename 拒绝路径分隔符与穿越；resolve 后必须仍在 artifacts 根目录内；
- 鉴权：走 main.py 中间件（Bearer，或 <img> 场景的 query token 回退）。
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

router = APIRouter()

_MEDIA_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".svg": "image/svg+xml",
    ".webp": "image/webp",
    ".pdf": "application/pdf",
}


@router.get("/api/artifacts/{session_id}/{filename}")
def get_artifact(session_id: str, filename: str):
    from agents.session_manager import get_session_manager

    if "/" in session_id or "\\" in session_id or ".." in session_id:
        raise HTTPException(status_code=400, detail="Invalid session_id")
    if not filename or filename in (".", "..") or "/" in filename or "\\" in filename:
        raise HTTPException(status_code=400, detail="Invalid filename")

    session = get_session_manager().get_or_load(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    cwd = session.projections.get("cwd")
    if not cwd:
        raise HTTPException(status_code=404, detail="Session has no workspace")

    base = (Path(cwd) / ".mycode" / "artifacts" / session_id).resolve()
    target = (base / filename).resolve()
    if not target.is_relative_to(base):
        raise HTTPException(status_code=400, detail="Path traversal rejected")
    if not target.is_file():
        raise HTTPException(status_code=404, detail="Artifact not found")

    return FileResponse(target, media_type=_MEDIA_TYPES.get(target.suffix.lower(), "application/octet-stream"))
