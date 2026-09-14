"""SSE events router.

提供 SSE 事件流和权限响应 API。
"""

from __future__ import annotations

import asyncio
import json
from typing import Optional

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

router = APIRouter(tags=["events"])


# SSE 只推送这些类型
_STREAM_TYPES = frozenset({
    "turn/start", "turn/end", "step/start", "step/end",
    "thinking", "text", "tool_call", "tool_result",
    "sub_agent/start", "sub_agent/end",
    "permission/request", "permission/resolved",
    "stats", "info", "error",
})


@router.get("/api/events")
async def api_events(
    request: Request,
    session_id: Optional[str] = None,
    since: int = 0,
):
    """SSE 事件流。"""
    from agents.session_manager import get_session_manager
    
    sm = get_session_manager()
    
    async def stream():
        unsubscribes = []
        merge_queue: asyncio.Queue = asyncio.Queue(maxsize=5000)
        tasks = []
        
        try:
            for session in sm.active_sessions():
                if session_id and session.id != session_id:
                    continue
                
                queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
                
                def on_event(event, q=queue):
                    if event["type"] in _STREAM_TYPES:
                        if event["seq"] >= since:
                            try:
                                q.put_nowait(event)
                            except asyncio.QueueFull:
                                pass
                
                unsubscribes.append(session.subscribe(on_event))
                
                async def forward(q):
                    try:
                        while True:
                            event = await q.get()
                            if event is None:
                                break
                            await merge_queue.put(event)
                    except asyncio.CancelledError:
                        pass
                
                tasks.append(asyncio.create_task(forward(queue)))
            
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(merge_queue.get(), timeout=30.0)
                except asyncio.TimeoutError:
                    yield ": heartbeat\n\n"
                    continue
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        finally:
            for u in unsubscribes:
                u()
            for t in tasks:
                t.cancel()
    
    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


class PermissionResponseData(BaseModel):
    rpc_id: str
    allowed: bool
    session_id: str


@router.post("/api/events/respond")
async def api_respond_permission(data: PermissionResponseData):
    """响应权限请求。"""
    from agents.session_manager import get_session_manager
    
    sm = get_session_manager()
    agent = sm.get_agent(data.session_id)
    if not agent:
        return {"success": False, "message": "Session not active"}
    
    return {"success": agent.respond_permission(data.rpc_id, data.allowed)}


@router.get("/api/sessions/{session_id}/messages")
async def api_session_messages(session_id: str):
    """获取 session 的消息历史（从事件日志派生）。"""
    from agents.session_manager import get_session_manager
    from agents.core.session import Session
    
    sm = get_session_manager()
    session = sm.get(session_id)
    if not session:
        session = Session.load_from_events(session_id)
    if not session:
        from fastapi import HTTPException
        raise HTTPException(404, "Session not found")
    
    return session.derive_messages()
