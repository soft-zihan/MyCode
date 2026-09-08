"""Trace logging APIs - 从 Phoenix 读取 OTel 数据。"""

from __future__ import annotations

import os
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter(tags=["trace"])


class TraceToggle(BaseModel):
    enabled: bool


def get_phoenix_endpoint() -> str:
    """获取 Phoenix API 端点。"""
    return os.environ.get("MYCODE_OTEL_ENDPOINT", "http://localhost:4317").replace(":4317", ":6006")


@router.get("/api/trace")
def api_trace_events(n: int = 50, session: Optional[str] = None) -> dict[str, Any]:
    otel_enabled = os.environ.get("MYCODE_OTEL", "").strip() not in ("", "0")
    phoenix_endpoint = get_phoenix_endpoint()

    phoenix_reachable = False
    if otel_enabled:
        try:
            import httpx
            with httpx.Client(timeout=2) as client:
                resp = client.get(f"{phoenix_endpoint}/health")
                phoenix_reachable = resp.status_code == 200
        except Exception:
            pass

    return {
        "enabled": otel_enabled,
        "path": phoenix_endpoint,
        "phoenix_endpoint": phoenix_endpoint,
        "phoenix_reachable": phoenix_reachable,
        "otel_enabled": otel_enabled,
        "session": session,
        "events": [],
    }


@router.get("/api/trace/files")
def api_trace_files() -> dict[str, Any]:
    """列出所有 session 事件文件（作为 trace 文件）。"""
    from pathlib import Path
    
    sessions_dir = Path.home() / ".mycode" / "sessions"
    files = []
    
    if sessions_dir.exists():
        for f in sorted(sessions_dir.glob("*.events.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True):
            stat = f.stat()
            session_id = f.stem.replace(".events", "")
            files.append({
                "session_id": session_id,
                "filename": f.name,
                "size": stat.st_size,
                "line_count": sum(1 for _ in open(f)),
                "created_at": stat.st_mtime,
            })
    
    return {"files": files}


@router.get("/api/trace/{session_id}")
def api_trace_session_events(session_id: str, n: int = 1000) -> dict[str, Any]:
    """Get trace events for a specific session from session event files."""
    from pathlib import Path
    import json
    
    sessions_dir = Path.home() / ".mycode" / "sessions"
    events_file = sessions_dir / f"{session_id}.events.jsonl"
    
    events = []
    if events_file.exists():
        with open(events_file) as f:
            for line in f:
                try:
                    event = json.loads(line.strip())
                    events.append(event)
                except json.JSONDecodeError:
                    continue
    
    # Limit to last n events
    if len(events) > n:
        events = events[-n:]
    
    phoenix_endpoint = get_phoenix_endpoint()
    
    return {
        "session_id": session_id,
        "phoenix_endpoint": phoenix_endpoint,
        "events": events,
        "event_count": len(events),
    }


@router.post("/api/trace/toggle")
def api_trace_toggle(data: TraceToggle) -> dict[str, bool]:
    # OTel 现在通过环境变量控制，不再支持运行时切换
    return {"enabled": data.enabled, "message": "OTel is controlled via MYCODE_OTEL environment variable"}
