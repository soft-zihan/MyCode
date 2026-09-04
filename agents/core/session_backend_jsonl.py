"""JSONL file-based session storage backend."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .session_backend import SessionBackend


class JsonlSessionBackend:
    """JSONL file-based session storage backend.
    
    Each session's events are stored in a separate JSONL file:
    ~/.mycode/sessions/{session_id}.events.jsonl
    """
    
    def __init__(self, session_dir: Path | None = None):
        if session_dir is None:
            from .session import session_dir as get_session_dir
            session_dir = get_session_dir()
        self.session_dir = session_dir
        self.session_dir.mkdir(parents=True, exist_ok=True)
    
    def _events_path(self, session_id: str) -> Path:
        return self.session_dir / f"{session_id}.events.jsonl"
    
    def append(self, session_id: str, event: dict[str, Any]) -> None:
        """Append an event to the session's event log."""
        path = self._events_path(session_id)
        line = json.dumps(event, ensure_ascii=False, default=str)
        with path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    
    def get_events(
        self,
        session_id: str,
        before: int | None = None,
        limit: int | None = None,
        from_seq: int | None = None,
        to_seq: int | None = None,
    ) -> list[dict[str, Any]]:
        """Get events from the session's event log."""
        path = self._events_path(session_id)
        if not path.exists():
            return []
        
        events = []
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                
                seq = event.get("seq", 0)
                
                # Apply filters
                if before is not None and seq >= before:
                    continue
                if from_seq is not None and seq < from_seq:
                    continue
                if to_seq is not None and seq >= to_seq:
                    continue
                
                events.append(event)
        
        # Apply limit (take the last N events)
        if limit is not None and len(events) > limit:
            events = events[-limit:]
        
        return events
    
    def load_all_events(self, session_id: str) -> list[dict[str, Any]]:
        """Load all events for a session."""
        return self.get_events(session_id)
    
    def truncate(self, session_id: str, keep_seq: int) -> None:
        """Truncate events after the given seq."""
        path = self._events_path(session_id)
        if not path.exists():
            return
        
        events = []
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event.get("seq", 0) < keep_seq:
                    events.append(event)
        
        # Rewrite the file
        with path.open("w", encoding="utf-8") as f:
            for event in events:
                f.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
    
    def delete_session(self, session_id: str) -> None:
        """Delete all events for a session."""
        path = self._events_path(session_id)
        if path.exists():
            path.unlink()
    
    def session_exists(self, session_id: str) -> bool:
        """Check if a session exists."""
        return self._events_path(session_id).exists()
    
    def get_event_count(self, session_id: str) -> int:
        """Get the total number of events for a session."""
        path = self._events_path(session_id)
        if not path.exists():
            return 0
        
        count = 0
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    count += 1
        return count
