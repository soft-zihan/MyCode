"""Session management APIs."""

from __future__ import annotations

import time
import copy
import uuid
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from agents.core.session import list_sessions, load_session, save_session, delete_session

router = APIRouter(tags=["sessions"])

_active_sessions: dict[str, dict] = {}


def get_active_sessions() -> dict[str, dict]:
    return _active_sessions


class SessionNameRequest(BaseModel):
    message: str


class SessionUpdateRequest(BaseModel):
    name: Optional[str] = None


class SteerRequest(BaseModel):
    message: str


class RewindRequest(BaseModel):
    turns: int = 1


class TruncateRequest(BaseModel):
    keep_user_messages: int


class PermissionModeRequest(BaseModel):
    mode: str


class PermissionResponseRequest(BaseModel):
    request_id: str
    allowed: bool


@router.get("/api/sessions")
def api_list_sessions() -> list[dict[str, Any]]:
    sessions = list_sessions()
    sessions.sort(key=lambda s: s.get("startTime", ""), reverse=True)
    return sessions


@router.get("/api/sessions/{session_id}")
def api_get_session(session_id: str) -> dict[str, Any]:
    data = load_session(session_id)
    if data is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return data


@router.delete("/api/sessions/{session_id}")
def api_delete_session(session_id: str) -> dict[str, bool]:
    success = delete_session(session_id)
    if not success:
        raise HTTPException(status_code=404, detail="Session not found")
    return {"success": True}


@router.post("/api/sessions/generate-name")
async def api_generate_session_name(data: SessionNameRequest) -> dict[str, str]:
    import asyncio
    fallback_name = data.message[:20] + "..." if len(data.message) > 20 else data.message
    try:
        from agents.agent import Agent
        from agents.core.agent_mode import BUILTIN_HIDDEN_AGENTS
        from agents.config import load_config
        from agents.service import AgentService
        
        title_config = BUILTIN_HIDDEN_AGENTS.get("title")
        if not title_config:
            return {"name": fallback_name}
        
        config = load_config()
        candidates = []
        side_model_id = config.routing.get('side')
        if side_model_id and side_model_id in config.endpoints:
            candidates.append(config.endpoints[side_model_id])
        for eid, ep in config.endpoints.items():
            if eid != side_model_id:
                candidates.append(ep)
        
        if not candidates:
            return {"name": fallback_name}
        
        last_error = None
        for endpoint in candidates:
            try:
                agent = Agent(
                    model=endpoint.model,
                    api_key=endpoint.api_key,
                    api_base=endpoint.base_url,
                    custom_system_prompt=title_config.system_prompt,
                )
                svc = AgentService(agent)
                await asyncio.wait_for(svc.run_once(data.message), timeout=10.0)
                name = svc.last_response.strip()
                if name:
                    name = name.replace('"', '').replace("'", "").strip()
                    return {"name": name[:30] if len(name) > 30 else name}
            except asyncio.TimeoutError:
                last_error = Exception(f"Timeout for model {endpoint.model}")
                continue
            except Exception as e:
                last_error = e
                continue
        
        if last_error:
            import logging
            logging.getLogger(__name__).warning(f"All endpoints failed for title generation: {last_error}")
        return {"name": fallback_name}
    except Exception:
        return {"name": fallback_name}


@router.put("/api/sessions/{session_id}")
def api_update_session(session_id: str, data: SessionUpdateRequest) -> dict[str, Any]:
    session_data = load_session(session_id)
    if session_data is None:
        raise HTTPException(status_code=404, detail="Session not found")
    
    if data.name is not None:
        session_data["metadata"]["name"] = data.name
    
    save_session(session_id, session_data)
    return {"success": True}


@router.post("/api/sessions/{session_id}/abort")
async def api_abort_session(session_id: str) -> dict[str, Any]:
    session_info = _active_sessions.get(session_id)
    if session_info and session_info.get("svc"):
        svc = session_info["svc"]
        svc.abort()
        return {"success": True, "message": "Abort requested"}
    return {"success": False, "message": "Session not active"}


@router.post("/api/sessions/{session_id}/compact")
async def api_compact_session(session_id: str) -> dict[str, Any]:
    session_data = load_session(session_id)
    if session_data is None:
        raise HTTPException(status_code=404, detail="Session not found")
    
    session_info = _active_sessions.get(session_id)
    if session_info and session_info.get("svc"):
        svc = session_info["svc"]
        try:
            await svc.compact()
            return {"success": True, "message": "Context compacted"}
        except Exception as e:
            return {"success": False, "message": str(e)}
    
    return {"success": False, "message": "Session not active"}


@router.post("/api/sessions/{session_id}/truncate")
async def api_truncate_session(session_id: str, data: TruncateRequest) -> dict[str, Any]:
    session_data = load_session(session_id)
    if session_data is None:
        raise HTTPException(status_code=404, detail="Session not found")
    
    openai_messages = session_data.get("openaiMessages", [])
    user_count = 0
    truncate_at = len(openai_messages)
    
    for i, msg in enumerate(openai_messages):
        if msg.get("role") == "user":
            if user_count >= data.keep_user_messages:
                truncate_at = i
                break
            user_count += 1
    
    session_data["openaiMessages"] = openai_messages[:truncate_at]
    
    turn_boundaries = session_data.get("turnBoundaries", [])
    session_data["turnBoundaries"] = [
        b for b in turn_boundaries 
        if b.get("message_count", 0) <= len(session_data["openaiMessages"])
    ]
    
    save_session(session_id, session_data)
    
    session_info = _active_sessions.get(session_id)
    if session_info and session_info.get("svc"):
        svc = session_info["svc"]
        messages = svc.get_messages()
        user_count = 0
        truncate_at = len(messages)
        for i, msg in enumerate(messages):
            if msg.get("role") == "user":
                if user_count >= data.keep_user_messages:
                    truncate_at = i
                    break
                user_count += 1
        svc.truncate_messages_to(truncate_at)
    
    return {"success": True, "message": f"Truncated to {data.keep_user_messages} user messages"}


@router.post("/api/sessions/{session_id}/rewind")
async def api_rewind_session(session_id: str, data: RewindRequest) -> dict[str, Any]:
    session_data = load_session(session_id)
    if session_data is None:
        raise HTTPException(status_code=404, detail="Session not found")
    
    turn_boundaries = session_data.get("turnBoundaries", [])
    if not turn_boundaries:
        return {"success": False, "message": "No turn boundaries found"}
    
    current_turn = len(turn_boundaries)
    target_turn = max(1, current_turn - data.turns)
    
    target_boundary = None
    for boundary in turn_boundaries:
        if boundary.get("turn") == target_turn:
            target_boundary = boundary
            break
    
    if not target_boundary:
        return {"success": False, "message": "Target turn not found"}
    
    message_count = target_boundary.get("message_count", 0)
    messages = session_data.get("openaiMessages", [])
    session_data["openaiMessages"] = messages[:message_count]
    
    checkpoint_count = target_boundary.get("checkpoint_count", 0)
    checkpoint_store = session_data.get("checkpointStore", {})
    snapshots = checkpoint_store.get("snapshots", [])
    checkpoint_store["snapshots"] = snapshots[:checkpoint_count]
    session_data["checkpointStore"] = checkpoint_store
    
    session_data["turnBoundaries"] = [b for b in turn_boundaries if b.get("turn") <= target_turn]
    
    save_session(session_id, session_data)
    return {
        "success": True, 
        "message": f"Rewound to turn {target_turn}",
        "turn": target_turn
    }


@router.put("/api/sessions/{session_id}/permission-mode")
async def api_update_permission_mode(session_id: str, data: PermissionModeRequest) -> dict[str, Any]:
    session_info = _active_sessions.get(session_id)
    if session_info and session_info.get("svc"):
        svc = session_info["svc"]
        svc.set_permission_mode(data.mode)
        return {"success": True, "permission_mode": data.mode}
    
    session_data = load_session(session_id)
    if session_data:
        session_data["permissionMode"] = data.mode
        save_session(session_id, session_data)
        return {"success": True, "permission_mode": data.mode}
    
    return {"success": False, "message": "Session not found"}


@router.post("/api/sessions/{session_id}/steer")
async def api_steer_session(session_id: str, data: SteerRequest) -> dict[str, Any]:
    session_info = _active_sessions.get(session_id)
    if session_info and session_info.get("svc"):
        svc = session_info["svc"]
        svc.steer(data.message)
        return {"success": True, "message": "Steer message queued"}
    
    return {"success": False, "message": "Session not active or steer not supported"}


@router.post("/api/sessions/{session_id}/fork")
async def api_fork_session(session_id: str) -> dict[str, Any]:
    session_data = load_session(session_id)
    if session_data is None:
        raise HTTPException(status_code=404, detail="Session not found")
    
    new_session_id = uuid.uuid4().hex[:8]
    new_session_data = copy.deepcopy(session_data)
    
    new_session_data["metadata"] = new_session_data.get("metadata", {}).copy()
    new_session_data["metadata"]["id"] = new_session_id
    new_session_data["metadata"]["startTime"] = time.strftime("%Y-%m-%dT%H:%M:%SZ")
    
    original_name = new_session_data["metadata"].get("name", "") or session_id
    base_name = original_name
    if "(fork " in original_name:
        base_name = original_name.rsplit("(fork ", 1)[0]
    
    all_sessions = list_sessions()
    fork_num = 1
    while True:
        fork_name = f"{base_name}(fork {fork_num})"
        exists = any(s.get("name") == fork_name for s in all_sessions)
        if not exists:
            break
        fork_num += 1
    
    new_session_data["metadata"]["name"] = fork_name
    save_session(new_session_id, new_session_data)
    
    return {
        "success": True,
        "new_session_id": new_session_id,
        "fork_name": fork_name,
        "message": f"Session forked to {new_session_id}"
    }


@router.post("/api/sessions/{session_id}/permission-response")
async def api_permission_response(session_id: str, data: PermissionResponseRequest) -> dict[str, Any]:
    session_info = _active_sessions.get(session_id)
    if session_info and session_info.get("svc"):
        svc = session_info["svc"]
        svc.respond_permission(data.request_id, data.allowed)
        return {"success": True}
    
    return {"success": False, "message": "Session not active"}


@router.get("/api/sessions/{session_id}/stats")
def api_session_stats(session_id: str) -> dict[str, Any]:
    session_info = _active_sessions.get(session_id)
    if session_info and session_info.get("svc"):
        svc = session_info["svc"]
        stats = svc.get_stats()
        agent = svc.agent
        return {
            "input_tokens": stats.get("input", 0),
            "output_tokens": stats.get("output", 0),
            "context_window": agent.context_window,
            "effective_window": agent.effective_window,
            "last_input_token_count": agent.last_input_token_count,
        }
    session_data = load_session(session_id)
    if session_data:
        return {
            "input_tokens": 0,
            "output_tokens": 0,
            "context_window": 128000,
            "effective_window": 108000,
            "last_input_token_count": 0,
        }
    raise HTTPException(status_code=404, detail="Session not found")
