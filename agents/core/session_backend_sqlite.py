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
    
    All sessions' events are stored in a single SQLite database.
    默认位置 ~/.mycode/sessions.db（session_dir() 的兄弟文件）；
    db_path=None 时每次访问动态解析——MYCODE_SESSION_DIR / HOME 的运行时
    重定向契约必须对整个进程生命周期有效（与 JsonlSessionBackend 相同，
    全局单例不能在构造时固化第一个调用方的路径）。
    """
    
    def __init__(self, db_path: Path | None = None):
        self._db_path_override = db_path
        self._initialized: set[str] = set()
    
    @property
    def db_path(self) -> Path:
        if self._db_path_override is not None:
            return Path(self._db_path_override)
        from .session import session_dir
        return session_dir().parent / "sessions.db"
    
    def _get_conn(self) -> sqlite3.Connection:
        """Get a database connection (lazy schema init, once per resolved path)."""
        path = self.db_path
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        key = str(path)
        if key not in self._initialized:
            conn.executescript(SCHEMA_SQL)
            cursor = conn.execute("SELECT version FROM schema_version LIMIT 1")
            if cursor.fetchone() is None:
                conn.execute(
                    "INSERT INTO schema_version (version) VALUES (?)",
                    (SCHEMA_VERSION,),
                )
            conn.commit()
            self._initialized.add(key)
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
    
    def list_session_ids(self) -> list[str]:
        """List all session IDs that have stored events."""
        conn = self._get_conn()
        try:
            cursor = conn.execute("SELECT DISTINCT session_id FROM events ORDER BY session_id")
            return [row[0] for row in cursor.fetchall()]
        finally:
            conn.close()
    
    def get_latest_event(self, session_id: str, event_type: str) -> dict[str, Any] | None:
        """Get the latest event of the given type for a session (None if absent).
        
        走 idx_events_type (session_id, type) 索引，O(log n)。
        """
        conn = self._get_conn()
        try:
            cursor = conn.execute(
                "SELECT data FROM events WHERE session_id = ? AND type = ? ORDER BY seq DESC LIMIT 1",
                (session_id, event_type),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            try:
                return json.loads(row[0])
            except json.JSONDecodeError:
                return None
        finally:
            conn.close()
    
    def get_last_event(self, session_id: str) -> dict[str, Any] | None:
        """Get the last event by seq (PRIMARY KEY 索引尾查，O(log n))."""
        conn = self._get_conn()
        try:
            cursor = conn.execute(
                "SELECT data FROM events WHERE session_id = ? ORDER BY seq DESC LIMIT 1",
                (session_id,),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            try:
                return json.loads(row[0])
            except json.JSONDecodeError:
                return None
        finally:
            conn.close()
