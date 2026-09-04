"""SQLite-based session storage backend."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .session_backend import SessionBackend


SCHEMA_VERSION = 1


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS events (
    session_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    type TEXT NOT NULL,
    time INTEGER NOT NULL,
    data TEXT NOT NULL,
    PRIMARY KEY (session_id, seq)
);

CREATE INDEX IF NOT EXISTS idx_events_session_seq ON events(session_id, seq);
CREATE INDEX IF NOT EXISTS idx_events_type ON events(session_id, type);
"""


class SqliteSessionBackend:
    """SQLite-based session storage backend.
    
    All sessions' events are stored in a single SQLite database:
    ~/.mycode/sessions.db
    """
    
    def __init__(self, db_path: Path | None = None):
        if db_path is None:
            db_path = Path.home() / ".mycode" / "sessions.db"
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()
    
    def _init_db(self) -> None:
        """Initialize the database schema."""
        conn = sqlite3.connect(self.db_path)
        try:
            conn.executescript(SCHEMA_SQL)
            
            # Check and set schema version
            cursor = conn.execute("SELECT version FROM schema_version LIMIT 1")
            row = cursor.fetchone()
            if row is None:
                conn.execute("INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
            conn.commit()
        finally:
            conn.close()
    
    def _get_conn(self) -> sqlite3.Connection:
        """Get a database connection."""
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn
    
    def append(self, session_id: str, event: dict[str, Any]) -> None:
        """Append an event to the session's event log."""
        conn = self._get_conn()
        try:
            data = json.dumps(event, ensure_ascii=False, default=str)
            conn.execute(
                "INSERT INTO events (session_id, seq, type, time, data) VALUES (?, ?, ?, ?, ?)",
                (session_id, event.get("seq", 0), event.get("type", ""), event.get("time", 0), data),
            )
            conn.commit()
        finally:
            conn.close()
    
    def get_events(
        self,
        session_id: str,
        before: int | None = None,
        limit: int | None = None,
        from_seq: int | None = None,
        to_seq: int | None = None,
    ) -> list[dict[str, Any]]:
        """Get events from the session's event log."""
        conn = self._get_conn()
        try:
            conditions = ["session_id = ?"]
            params: list[Any] = [session_id]
            
            if before is not None:
                conditions.append("seq < ?")
                params.append(before)
            if from_seq is not None:
                conditions.append("seq >= ?")
                params.append(from_seq)
            if to_seq is not None:
                conditions.append("seq < ?")
                params.append(to_seq)
            
            where_clause = " AND ".join(conditions)
            
            if limit is not None:
                # Get the last N events
                query = f"""
                    SELECT data FROM events
                    WHERE {where_clause}
                    ORDER BY seq DESC
                    LIMIT ?
                """
                params.append(limit)
                cursor = conn.execute(query, params)
                rows = cursor.fetchall()
                # Reverse to get chronological order
                rows = list(reversed(rows))
            else:
                query = f"""
                    SELECT data FROM events
                    WHERE {where_clause}
                    ORDER BY seq ASC
                """
                cursor = conn.execute(query, params)
                rows = cursor.fetchall()
            
            events = []
            for (data,) in rows:
                try:
                    events.append(json.loads(data))
                except json.JSONDecodeError:
                    continue
            
            return events
        finally:
            conn.close()
    
    def load_all_events(self, session_id: str) -> list[dict[str, Any]]:
        """Load all events for a session."""
        return self.get_events(session_id)
    
    def truncate(self, session_id: str, keep_seq: int) -> None:
        """Truncate events after the given seq."""
        conn = self._get_conn()
        try:
            conn.execute(
                "DELETE FROM events WHERE session_id = ? AND seq >= ?",
                (session_id, keep_seq),
            )
            conn.commit()
        finally:
            conn.close()
    
    def delete_session(self, session_id: str) -> None:
        """Delete all events for a session."""
        conn = self._get_conn()
        try:
            conn.execute("DELETE FROM events WHERE session_id = ?", (session_id,))
            conn.commit()
        finally:
            conn.close()
    
    def session_exists(self, session_id: str) -> bool:
        """Check if a session exists."""
        conn = self._get_conn()
        try:
            cursor = conn.execute(
                "SELECT 1 FROM events WHERE session_id = ? LIMIT 1",
                (session_id,),
            )
            return cursor.fetchone() is not None
        finally:
            conn.close()
    
    def get_event_count(self, session_id: str) -> int:
        """Get the total number of events for a session."""
        conn = self._get_conn()
        try:
            cursor = conn.execute(
                "SELECT COUNT(*) FROM events WHERE session_id = ?",
                (session_id,),
            )
            row = cursor.fetchone()
            return row[0] if row else 0
        finally:
            conn.close()
