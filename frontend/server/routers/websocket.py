"""WebSocket endpoint for real-time event streaming.

Replaces SSE with a single WebSocket connection that multiplexes events
from all sessions. Events are routed by session_id on the frontend.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect


router = APIRouter()


class Subscriber:
    """WebSocket subscriber."""
    
    def __init__(self, websocket: WebSocket) -> None:
        self.id = uuid.uuid4().hex
        self.websocket = websocket
        self.subscribed_sessions: set[str] = set()  # empty = subscribe to all


# Global list of active subscribers
_subscribers: list[Subscriber] = []


@router.websocket("/ws/events")
async def websocket_events(websocket: WebSocket) -> None:
    """WebSocket endpoint for real-time event streaming.
    
    Clients can send commands:
    - {"type": "subscribe", "session_id": "..."} - subscribe to specific session
    - {"type": "unsubscribe", "session_id": "..."} - unsubscribe from session
    - {"type": "abort", "session_id": "..."} - abort a session
    
    Server broadcasts all events to subscribers.
    """
    await websocket.accept()
    subscriber = Subscriber(websocket)
    _subscribers.append(subscriber)
    print(f"[WS] subscriber added: {subscriber.id[:8]}, total: {len(_subscribers)}")
    
    try:
        while True:
            # Receive commands from client
            data = await websocket.receive_json()
            cmd = data.get("type")
            
            if cmd == "subscribe":
                session_id = data.get("session_id")
                if session_id:
                    subscriber.subscribed_sessions.add(session_id)
            
            elif cmd == "unsubscribe":
                session_id = data.get("session_id")
                if session_id:
                    subscriber.subscribed_sessions.discard(session_id)
            
            elif cmd == "abort":
                session_id = data.get("session_id")
                if session_id:
                    # Import here to avoid circular dependency
                    from agents.session_manager import get_session_manager
                    sm = get_session_manager()
                    sm.abort(session_id)
    
    except WebSocketDisconnect:
        print(f"[WS] subscriber disconnected: {subscriber.id[:8]}")
    except Exception as e:
        print(f"[WS] error: {e}")
    finally:
        if subscriber in _subscribers:
            _subscribers.remove(subscriber)
            print(f"[WS] subscriber removed: {subscriber.id[:8]}, total: {len(_subscribers)}")


def broadcast_event(event: dict[str, Any], target_session_id: str | None = None) -> None:
    """Broadcast event to WebSocket subscribers.
    
    Called by Session.append() to push events to connected clients.
    Events are filtered by subscriber's subscribed_sessions.
    
    Args:
        event: The event to broadcast
        target_session_id: If provided, only send to subscribers who subscribed to this session.
                          If None, send to all subscribers (for session/created events).
    """
    event_type = event.get("type", "unknown")
    session_id = event.get("session_id", "unknown")
    
    if not _subscribers:
        print(f"[WS] broadcast: no subscribers for {event_type} session={session_id[:8] if session_id else 'N/A'}")
        return
    
    # For global events, send to all subscribers.
    # For session events, only send to subscribers of the target session.
    is_global_event = event_type == "session/created" or event_type.startswith("eval/")
    
    # Use target_session_id for filtering, not event's session_id
    # This is important for sub-agent events where event.session_id is the sub-agent's id
    # but we want to send to subscribers of the parent session
    filter_session_id = target_session_id or session_id
    
    print(f"[WS] broadcast: {event_type} session={session_id[:8] if session_id else 'N/A'} target={filter_session_id[:8] if filter_session_id else 'all'} to {len(_subscribers)} subscribers")
    
    for subscriber in _subscribers[:]:  # copy list to avoid concurrent modification
        # Global events and unsubscribed clients receive all matching traffic.
        should_send = (
            is_global_event or
            not subscriber.subscribed_sessions or
            filter_session_id in subscriber.subscribed_sessions
        )
        
        if should_send:
            try:
                # Schedule send as async task to avoid blocking
                task = asyncio.create_task(subscriber.websocket.send_json(event))
                print(f"[WS] broadcast: created task for subscriber {subscriber.id[:8]}")
            except Exception as e:
                print(f"[WS] broadcast error: {e}")
                # Remove broken subscriber
                if subscriber in _subscribers:
                    _subscribers.remove(subscriber)
