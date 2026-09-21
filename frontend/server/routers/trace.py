"""Trace logging APIs - Langfuse status + session event logs."""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter

from agents.core.session import session_dir
from agents.logging import print_error
from agents.observability.status import get_langfuse_status

router = APIRouter(tags=["trace"])


def _load_trace_timeline_events(session_id: str, limit: int) -> list[dict[str, Any]]:
    import json
    from datetime import datetime

    events_file = session_dir() / f"{session_id}.events.jsonl"
    if not events_file.exists():
        return []

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

    events: list[dict[str, Any]] = []
    with events_file.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError as exc:
                print_error(f"[trace] invalid JSONL line in {events_file.name}: {exc}")
                continue

            event_type = raw.get("type", "")
            timestamp = raw.get("time", 0)
            if isinstance(timestamp, (int, float)) and timestamp > 1e12:
                timestamp = timestamp / 1000
            ts = datetime.fromtimestamp(timestamp).isoformat(timespec="milliseconds") if timestamp else ""

            event = {
                "ts": ts,
                "kind": kind_map.get(event_type, event_type.replace("/", ".")),
                "type": event_type,
                **raw,
            }

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

    return events[-limit:] if limit and len(events) > limit else events


@router.get("/api/trace/status")
def api_trace_status(force: bool = False) -> dict[str, Any]:
    return get_langfuse_status(force=force)


@router.get("/api/trace")
def api_trace_events(n: int = 50, session: Optional[str] = None) -> dict[str, Any]:
    events = _load_trace_timeline_events(session, n) if session else []

    return {
        "session": session,
        "events": events,
    }

