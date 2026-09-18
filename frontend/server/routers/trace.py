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

    events = []
    if session:
        from pathlib import Path
        import json
        from datetime import datetime
        sessions_dir = Path.home() / ".mycode" / "sessions"
        events_file = sessions_dir / f"{session}.events.jsonl"
        if events_file.exists():
            with open(events_file) as f:
                for line in f:
                    try:
                        raw = json.loads(line.strip())
                        # 转换格式：JSONL {type, time} -> TracePage {kind, ts}
                        event_type = raw.get("type", "")
                        timestamp = raw.get("time", 0)
                        # 转换毫秒时间戳为 ISO 格式
                        if timestamp > 1e12:
                            timestamp = timestamp / 1000
                        ts = datetime.fromtimestamp(timestamp).isoformat(timespec='milliseconds')
                        
                        # 映射事件类型
                        kind_map = {
                            "turn/start": "turn.start",
                            "turn/end": "turn.end",
                            "step/start": "step.start",
                            "step/end": "step.end",
                            "user_message": "turn.start",
                            "assistant_message": "turn.end",
                            "tool_call": "tool_call.start",
                            "tool_result": "tool_call.end",
                            "stats": "model_call.end",
                        }
                        kind = kind_map.get(event_type, event_type.replace("/", "."))
                        
                        # 构建 TracePage 期望的格式
                        event = {
                            "ts": ts,
                            "kind": kind,
                            "type": event_type,
                            **raw,
                        }
                        
                        # 添加一些额外字段供 TracePage 使用
                        if event_type == "stats":
                            event["input_tokens"] = raw.get("input_tokens", 0)
                            event["output_tokens"] = raw.get("output_tokens", 0)
                            event["duration_s"] = 0
                        elif event_type == "tool_result":
                            event["tool"] = raw.get("name", "")
                            event["success"] = raw.get("status") == "success"
                            event["duration_s"] = (raw.get("duration_ms", 0) or 0) / 1000
                            event["preview"] = str(raw.get("result", ""))[:100]
                        elif event_type == "tool_call":
                            event["tool"] = raw.get("name", "")
                            event["input"] = str(raw.get("input", ""))[:50]
                        
                        events.append(event)
                    except (json.JSONDecodeError, Exception):
                        continue
            if len(events) > n:
                events = events[-n:]

    return {
        "enabled": otel_enabled,
        "path": phoenix_endpoint,
        "phoenix_endpoint": phoenix_endpoint,
        "phoenix_reachable": phoenix_reachable,
        "otel_enabled": otel_enabled,
        "session": session,
        "events": events,
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


@router.get("/api/trace/{session_id}/file-snapshots")
def api_trace_file_snapshots(session_id: str) -> dict[str, Any]:
    """Get file snapshots from session events (for historical sessions)."""
    from pathlib import Path
    import json

    sessions_dir = Path.home() / ".mycode" / "sessions"
    events_file = sessions_dir / f"{session_id}.events.jsonl"

    snapshots = {}  # file_path -> {file_path, is_new, old_content, new_content}

    if events_file.exists():
        with open(events_file) as f:
            for line in f:
                try:
                    event = json.loads(line.strip())
                    if event.get("type") == "tool_result" and event.get("snapshot"):
                        snap = event["snapshot"]
                        if "file_path" in snap and "old_content" in snap and "new_content" in snap:
                            # Keep the latest snapshot for each file
                            snapshots[snap["file_path"]] = {
                                "file_path": snap["file_path"],
                                "is_new": snap.get("is_new", False),
                                "old_content": snap["old_content"],
                                "new_content": snap["new_content"],
                            }
                except (json.JSONDecodeError, KeyError):
                    continue

    return {"snapshots": list(snapshots.values())}


@router.post("/api/trace/toggle")
def api_trace_toggle(data: TraceToggle) -> dict[str, bool]:
    # OTel 现在通过环境变量控制，不再支持运行时切换
    return {"enabled": data.enabled, "message": "OTel is controlled via MYCODE_OTEL environment variable"}
