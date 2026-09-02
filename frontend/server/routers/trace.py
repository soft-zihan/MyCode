"""Trace logging APIs."""

from __future__ import annotations

import json
import re
from typing import Any, Optional

from fastapi import APIRouter
from pydantic import BaseModel

from agents.observability.trace import recent_events, trace_enabled, set_trace_enabled, trace_path, trace_dir

router = APIRouter(tags=["trace"])


class TraceToggle(BaseModel):
    enabled: bool


@router.get("/api/trace")
def api_trace_events(n: int = 50, session: Optional[str] = None) -> dict[str, Any]:
    """获取 trace 事件。如果指定 session，读取该 session 的文件；否则读取当前 session。"""
    if session:
        trace_file = None
        for f in trace_dir().glob(f"*_{session}.jsonl"):
            trace_file = f
            break
        if not trace_file:
            trace_file = trace_dir() / f"{session}.jsonl"
        
        events = []
        if trace_file.exists():
            with open(trace_file, "r", encoding="utf-8") as f:
                lines = f.readlines()
                for line in lines[-n:]:
                    try:
                        events.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
        return {
            "enabled": trace_enabled(),
            "path": str(trace_file),
            "session": session,
            "events": events,
        }
    else:
        events = recent_events(n)
        return {
            "enabled": trace_enabled(),
            "path": str(trace_path()),
            "events": events,
        }


@router.get("/api/trace/files")
def api_trace_files() -> dict[str, Any]:
    """列出所有 trace 文件，按时间倒序。"""
    files = []
    td = trace_dir()
    if not td.exists():
        return {"files": files}
    
    for f in td.glob("*.jsonl"):
        name = f.stem
        match = re.match(r"^(\d{8}_\d{6})_(.+)$", name)
        if match:
            ts, session_id = match.groups()
            created_at = f"{ts[:4]}-{ts[4:6]}-{ts[6:8]} {ts[9:11]}:{ts[11:13]}:{ts[13:15]}"
        else:
            session_id = name
            created_at = None
        
        stat = f.stat()
        line_count = 0
        try:
            with open(f, "r", encoding="utf-8") as fp:
                line_count = sum(1 for _ in fp)
        except Exception:
            pass
        
        files.append({
            "filename": f.name,
            "session_id": session_id,
            "created_at": created_at,
            "size": stat.st_size,
            "line_count": line_count,
            "modified": stat.st_mtime,
        })
    
    files.sort(key=lambda x: x["modified"], reverse=True)
    return {"files": files}


@router.get("/api/trace/{session_id}")
def api_trace_session_events(session_id: str, n: int = 1000) -> dict[str, Any]:
    """Get trace events for a specific session."""
    trace_file = None
    for f in trace_dir().glob(f"*_{session_id}.jsonl"):
        trace_file = f
        break
    if not trace_file:
        trace_file = trace_dir() / f"{session_id}.jsonl"
    
    events = []
    if trace_file.exists():
        with open(trace_file, "r", encoding="utf-8") as f:
            lines = f.readlines()
            for line in lines[-n:]:
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return {
        "session_id": session_id,
        "events": events,
    }


@router.post("/api/trace/toggle")
def api_trace_toggle(data: TraceToggle) -> dict[str, bool]:
    set_trace_enabled(data.enabled)
    return {"enabled": data.enabled}
