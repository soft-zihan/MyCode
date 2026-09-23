"""Session storage backend abstraction."""

from __future__ import annotations

from typing import Any, Protocol


class SessionBackend(Protocol):
    """Session storage backend interface.
    
    All storage backends must implement this interface.
    """
    
    def append(self, session_id: str, event: dict[str, Any]) -> None:
        """Append an event to the session's event log."""
        ...
    
    def get_events(
        self,
        session_id: str,
        before: int | None = None,
        limit: int | None = None,
        from_seq: int | None = None,
        to_seq: int | None = None,
    ) -> list[dict[str, Any]]:
        """Get events from the session's event log.
        
        Args:
            session_id: Session ID
            before: Get events with seq < before
            limit: Maximum number of events to return
            from_seq: Get events with seq >= from_seq
            to_seq: Get events with seq < to_seq
        
        Returns:
            List of events
        """
        ...
    
    def load_all_events(self, session_id: str) -> list[dict[str, Any]]:
        """Load all events for a session."""
        ...
    
    def truncate(self, session_id: str, keep_seq: int) -> None:
        """Truncate events after the given seq."""
        ...
    
    def delete_session(self, session_id: str) -> None:
        """Delete all events for a session."""
        ...
    
    def session_exists(self, session_id: str) -> bool:
        """Check if a session exists."""
        ...
    
    def get_event_count(self, session_id: str) -> int:
        """Get the total number of events for a session."""
        ...
    
    def list_session_ids(self) -> list[str]:
        """List all session IDs that have stored events."""
        ...
    
    def get_latest_event(self, session_id: str, event_type: str) -> dict[str, Any] | None:
        """Get the latest event of the given type for a session (None if absent)."""
        ...
    
    def get_last_event(self, session_id: str) -> dict[str, Any] | None:
        """Get the last (highest seq) event of a session, efficiently (None if absent).
        
        用途：max_seq 探测与最后活动时间（event["time"]，毫秒 epoch）。
        """
        ...
