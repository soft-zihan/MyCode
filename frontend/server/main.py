#!/usr/bin/env python3
"""FastAPI server that wraps existing CLI modules to provide HTTP API for frontend."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel
import asyncio

# Add project root to path so we can import agents
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

from agents.core.session import list_sessions, load_session, save_session, delete_session
from agents.memory.memory import list_memories, save_memory, delete_memory, get_memory_dir
from agents.skills.skills import discover_skills, get_skill_by_name, skill_stats
from agents.observability.trace import recent_events, trace_enabled, set_trace_enabled, trace_path
from agents.config import (
    load_config, save_config, list_endpoints, get_primary_endpoint,
    AppConfig, ModelEndpointConfig
)
from agents.core.subagent import get_sub_agent_config, _discover_custom_agents

app = FastAPI(title="MyCode API", version="1.0.0")

# CORS for local development
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Session APIs ──────────────────────────────────────────────────────────────

@app.get("/api/sessions")
def api_list_sessions() -> list[dict[str, Any]]:
    """List all sessions with metadata."""
    sessions = list_sessions()
    # Sort by startTime descending
    sessions.sort(key=lambda s: s.get("startTime", ""), reverse=True)
    return sessions


@app.get("/api/sessions/{session_id}")
def api_get_session(session_id: str) -> dict[str, Any]:
    """Get full session data including messages."""
    data = load_session(session_id)
    if data is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return data


@app.delete("/api/sessions/{session_id}")
def api_delete_session(session_id: str) -> dict[str, bool]:
    """Delete a session."""
    success = delete_session(session_id)
    if not success:
        raise HTTPException(status_code=404, detail="Session not found")
    return {"success": True}


class SessionNameRequest(BaseModel):
    message: str


@app.post("/api/sessions/generate-name")
async def api_generate_session_name(data: SessionNameRequest) -> dict[str, str]:
    """Generate a session name from user message using title agent."""
    import asyncio
    fallback_name = data.message[:20] + "..." if len(data.message) > 20 else data.message
    try:
        from agents.agent import Agent
        from agents.core.agent_mode import BUILTIN_HIDDEN_AGENTS
        
        # Get title agent config
        title_config = BUILTIN_HIDDEN_AGENTS.get("title")
        if not title_config:
            return {"name": fallback_name}
        
        # Load config to get API credentials
        from agents.config import load_config
        config = load_config()
        
        # Collect candidate endpoints: side model first, then all others
        candidates = []
        side_model_id = config.routing.get('side')
        if side_model_id and side_model_id in config.endpoints:
            candidates.append(config.endpoints[side_model_id])
        for eid, ep in config.endpoints.items():
            if eid != side_model_id:
                candidates.append(ep)
        
        if not candidates:
            return {"name": fallback_name}
        
        # Try each endpoint until one succeeds (with timeout)
        last_error = None
        for endpoint in candidates:
            try:
                agent = Agent(
                    model=endpoint.model,
                    api_key=endpoint.api_key,
                    api_base=endpoint.base_url,
                    custom_system_prompt=title_config.system_prompt,
                )
                # Add timeout to prevent hanging
                result = await asyncio.wait_for(agent.run_once(data.message), timeout=10.0)
                name = result.get("text", "").strip()
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


class SessionUpdateRequest(BaseModel):
    name: str | None = None
    frontendMessages: list[dict[str, Any]] | None = None


@app.put("/api/sessions/{session_id}")
def api_update_session(session_id: str, data: SessionUpdateRequest) -> dict[str, Any]:
    """Update session metadata (e.g., name) or save frontend-processed messages."""
    session_data = load_session(session_id)
    if session_data is None:
        raise HTTPException(status_code=404, detail="Session not found")
    
    if data.name is not None:
        session_data["metadata"]["name"] = data.name
    
    # Save frontend-processed messages for lossless restore
    if data.frontendMessages is not None:
        session_data["frontendMessages"] = data.frontendMessages
    
    save_session(session_id, session_data)
    return {"success": True}


# ── Session Control Endpoints ─────────────────────────────────────────────────

# In-memory store for active sessions (for abort, steer, etc.)
_active_sessions: dict[str, dict] = {}


class SteerRequest(BaseModel):
    message: str


class RewindRequest(BaseModel):
    turns: int = 1


class TruncateRequest(BaseModel):
    keep_user_messages: int  # 保留前 N 条用户消息


class PermissionModeRequest(BaseModel):
    mode: str  # "default", "bypassPermissions", "plan"


@app.post("/api/sessions/{session_id}/abort")
async def api_abort_session(session_id: str) -> dict[str, Any]:
    """Abort a running session."""
    session_info = _active_sessions.get(session_id)
    if session_info and session_info.get("agent"):
        agent = session_info["agent"]
        agent.request_abort()
        return {"success": True, "message": "Abort requested"}
    return {"success": False, "message": "Session not active"}


@app.post("/api/sessions/{session_id}/compact")
async def api_compact_session(session_id: str) -> dict[str, Any]:
    """Compact session context."""
    session_data = load_session(session_id)
    if session_data is None:
        raise HTTPException(status_code=404, detail="Session not found")
    
    session_info = _active_sessions.get(session_id)
    if session_info and session_info.get("agent"):
        agent = session_info["agent"]
        try:
            await agent.compact_context("Manual compaction requested")
            return {"success": True, "message": "Context compacted"}
        except Exception as e:
            return {"success": False, "message": str(e)}
    
    return {"success": False, "message": "Session not active"}


@app.post("/api/sessions/{session_id}/truncate")
async def api_truncate_session(session_id: str, data: TruncateRequest) -> dict[str, Any]:
    """Truncate session messages to keep only the first N user messages."""
    session_data = load_session(session_id)
    if session_data is None:
        raise HTTPException(status_code=404, detail="Session not found")
    
    # Truncate openaiMessages by counting user messages
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
    
    # Truncate frontendMessages if exists (by counting user messages)
    frontend_messages = session_data.get("frontendMessages", [])
    if frontend_messages:
        user_count = 0
        truncate_at = len(frontend_messages)
        
        for i, msg in enumerate(frontend_messages):
            if msg.get("role") == "user":
                if user_count >= data.keep_user_messages:
                    truncate_at = i
                    break
                user_count += 1
        
        session_data["frontendMessages"] = frontend_messages[:truncate_at]
    
    # Truncate turnBoundaries
    turn_boundaries = session_data.get("turnBoundaries", [])
    session_data["turnBoundaries"] = [
        b for b in turn_boundaries 
        if b.get("message_count", 0) <= len(session_data["openaiMessages"])
    ]
    
    save_session(session_id, session_data)
    
    # Also truncate active agent's messages if running
    session_info = _active_sessions.get(session_id)
    if session_info and session_info.get("agent"):
        from agents.service import AgentService
        svc = AgentService(session_info["agent"])
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


@app.post("/api/sessions/{session_id}/rewind")
async def api_rewind_session(session_id: str, data: RewindRequest) -> dict[str, Any]:
    """Rewind session by N turns."""
    session_data = load_session(session_id)
    if session_data is None:
        raise HTTPException(status_code=404, detail="Session not found")
    
    # Get turn boundaries
    turn_boundaries = session_data.get("turnBoundaries", [])
    if not turn_boundaries:
        return {"success": False, "message": "No turn boundaries found"}
    
    # Find the target turn
    current_turn = len(turn_boundaries)
    target_turn = max(1, current_turn - data.turns)
    
    # Find the boundary for the target turn
    target_boundary = None
    for boundary in turn_boundaries:
        if boundary.get("turn") == target_turn:
            target_boundary = boundary
            break
    
    if not target_boundary:
        return {"success": False, "message": "Target turn not found"}
    
    # Truncate messages
    message_count = target_boundary.get("message_count", 0)
    messages = session_data.get("openaiMessages", [])
    session_data["openaiMessages"] = messages[:message_count]
    
    # Restore checkpoints if available
    checkpoint_count = target_boundary.get("checkpoint_count", 0)
    checkpoint_store = session_data.get("checkpointStore", {})
    snapshots = checkpoint_store.get("snapshots", [])
    checkpoint_store["snapshots"] = snapshots[:checkpoint_count]
    session_data["checkpointStore"] = checkpoint_store
    
    # Update turn boundaries
    session_data["turnBoundaries"] = [b for b in turn_boundaries if b.get("turn") <= target_turn]
    
    save_session(session_id, session_data)
    return {
        "success": True, 
        "message": f"Rewound to turn {target_turn}",
        "turn": target_turn
    }


@app.put("/api/sessions/{session_id}/permission-mode")
async def api_update_permission_mode(session_id: str, data: PermissionModeRequest) -> dict[str, Any]:
    """Update permission mode for a session."""
    session_info = _active_sessions.get(session_id)
    if session_info and session_info.get("agent"):
        from agents.service import AgentService
        svc = AgentService(session_info["agent"])
        svc.set_permission_mode(data.mode)
        return {"success": True, "permission_mode": data.mode}
    
    # If session not active, update stored session data
    session_data = load_session(session_id)
    if session_data:
        session_data["permissionMode"] = data.mode
        save_session(session_id, session_data)
        return {"success": True, "permission_mode": data.mode}
    
    return {"success": False, "message": "Session not found"}


@app.post("/api/sessions/{session_id}/steer")
async def api_steer_session(session_id: str, data: SteerRequest) -> dict[str, Any]:
    """Inject a steering message into a running session."""
    session_info = _active_sessions.get(session_id)
    if session_info and session_info.get("agent"):
        from agents.service import AgentService
        svc = AgentService(session_info["agent"])
        svc.steer(data.message)
        return {"success": True, "message": "Steer message queued"}
    
    return {"success": False, "message": "Session not active or steer not supported"}


@app.post("/api/sessions/{session_id}/fork")
async def api_fork_session(session_id: str) -> dict[str, Any]:
    """Fork a session (create a deep copy with a new ID)."""
    session_data = load_session(session_id)
    if session_data is None:
        raise HTTPException(status_code=404, detail="Session not found")
    
    # Generate new session ID
    import uuid
    new_session_id = uuid.uuid4().hex[:8]
    
    # Deep copy session data to avoid shared references
    import copy
    new_session_data = copy.deepcopy(session_data)
    
    # Update metadata
    new_session_data["metadata"] = new_session_data.get("metadata", {}).copy()
    new_session_data["metadata"]["id"] = new_session_id
    new_session_data["metadata"]["startTime"] = time.strftime("%Y-%m-%dT%H:%M:%SZ")
    new_session_data["metadata"]["name"] = ""  # Clear name for forked session
    
    # Save new session
    save_session(new_session_id, new_session_data)
    
    return {
        "success": True,
        "new_session_id": new_session_id,
        "message": f"Session forked to {new_session_id}"
    }


class PermissionResponseRequest(BaseModel):
    request_id: str
    allowed: bool


@app.post("/api/sessions/{session_id}/permission-response")
async def api_permission_response(session_id: str, data: PermissionResponseRequest) -> dict[str, Any]:
    """Respond to a permission request."""
    session_info = _active_sessions.get(session_id)
    if session_info and session_info.get("agent"):
        agent = session_info["agent"]
        agent.set_permission_response(data.request_id, data.allowed)
        return {"success": True}
    
    return {"success": False, "message": "Session not active"}


class RevertRequest(BaseModel):
    file_path: str
    old_content: str


@app.post("/api/revert")
def api_revert_file(data: RevertRequest) -> dict[str, Any]:
    """Revert a file to its previous content (from snapshot)."""
    try:
        from agents.tools import _resolve_tool_path
        target = _resolve_tool_path(data.file_path, must_exist=False)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(data.old_content)
        return {"success": True, "file_path": data.file_path}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── Memory APIs ───────────────────────────────────────────────────────────────

@app.get("/api/memories")
def api_list_memories() -> list[dict[str, str]]:
    """List all memory entries."""
    entries = list_memories()
    return [
        {
            "name": e.name,
            "description": e.description,
            "type": e.type,
            "filename": e.filename,
            "content": e.content,
        }
        for e in entries
    ]


@app.get("/api/memories/{filename}")
def api_get_memory(filename: str) -> dict[str, str]:
    """Get a specific memory entry by filename."""
    entries = list_memories()
    for e in entries:
        if e.filename == filename:
            return {
                "name": e.name,
                "description": e.description,
                "type": e.type,
                "filename": e.filename,
                "content": e.content,
            }
    raise HTTPException(status_code=404, detail="Memory not found")


class MemoryCreate(BaseModel):
    name: str
    description: str
    type: str
    content: str


@app.post("/api/memories")
def api_create_memory(data: MemoryCreate) -> dict[str, str]:
    """Create a new memory entry."""
    if data.type not in {"user", "feedback", "project", "reference"}:
        raise HTTPException(status_code=400, detail="Invalid memory type")
    filename = save_memory(data.name, data.description, data.type, data.content)
    return {"filename": filename}


@app.delete("/api/memories/{filename}")
def api_delete_memory(filename: str) -> dict[str, bool]:
    """Delete a memory entry."""
    success = delete_memory(filename)
    if not success:
        raise HTTPException(status_code=404, detail="Memory not found")
    return {"success": True}


class MemoryUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    content: str | None = None
    hooks: list[str] | None = None


@app.put("/api/memories/{filename}")
def api_update_memory(filename: str, data: MemoryUpdate) -> dict[str, str]:
    """Update a memory entry (rename, edit content, set hooks)."""
    from agents.memory.memory import get_memory_dir
    memory_dir = get_memory_dir()
    memory_path = memory_dir / filename
    
    if not memory_path.exists():
        raise HTTPException(status_code=404, detail="Memory not found")
    
    # Read existing content
    existing = memory_path.read_text(encoding="utf-8")
    
    # Parse frontmatter if exists
    import re
    fm_match = re.match(r'^---\n(.*?)\n---\n(.*)$', existing, re.DOTALL)
    
    if fm_match:
        fm_text = fm_match.group(1)
        body = fm_match.group(2)
        # Parse existing frontmatter
        lines = fm_text.split('\n')
        fm_dict = {}
        for line in lines:
            if ':' in line:
                key, val = line.split(':', 1)
                fm_dict[key.strip()] = val.strip().strip('"').strip("'")
    else:
        fm_dict = {}
        body = existing
    
    # Update fields
    if data.name is not None:
        fm_dict['name'] = data.name
    if data.description is not None:
        fm_dict['description'] = data.description
    if data.hooks is not None:
        fm_dict['hooks'] = ','.join(data.hooks)
    
    # Build new content
    new_body = data.content if data.content is not None else body
    new_fm = '\n'.join(f'{k}: {v}' for k, v in fm_dict.items())
    new_content = f'---\n{new_fm}\n---\n{new_body}'
    
    # If name changed, rename file
    if data.name is not None and data.name != fm_dict.get('name', ''):
        new_filename = f"{data.name.lower().replace(' ', '_')}.md"
        new_path = memory_dir / new_filename
        new_path.write_text(new_content, encoding="utf-8")
        memory_path.unlink()
        return {"filename": new_filename, "message": "Memory updated and renamed"}
    else:
        memory_path.write_text(new_content, encoding="utf-8")
        return {"filename": filename, "message": "Memory updated"}


# ── Skills APIs ───────────────────────────────────────────────────────────────

@app.get("/api/skills")
def api_list_skills() -> list[dict[str, Any]]:
    """List all skills with source level."""
    skills = discover_skills()
    return [
        {
            "name": getattr(s, 'name', 'unknown'),
            "description": getattr(s, 'description', ''),
            "skill_dir": getattr(s, 'skill_dir', ''),
            "model": getattr(s, 'model', None),
            "source": getattr(s, 'source', 'project'),
        }
        for s in skills
    ]


@app.get("/api/skills/{skill_name}")
def api_get_skill(skill_name: str) -> dict[str, Any]:
    """Get skill details including raw file content."""
    skill = get_skill_by_name(skill_name)
    if skill is None:
        raise HTTPException(status_code=404, detail="Skill not found")
    
    # Read raw SKILL.md content
    skill_dir = getattr(skill, 'skill_dir', '')
    raw_content = ''
    if skill_dir:
        skill_file = Path(skill_dir) / 'SKILL.md'
        if skill_file.exists():
            raw_content = skill_file.read_text()
    
    return {
        "name": getattr(skill, 'name', 'unknown'),
        "description": getattr(skill, 'description', ''),
        "skill_dir": skill_dir,
        "model": getattr(skill, 'model', None),
        "source": getattr(skill, 'source', 'project'),
        "prompt_template": getattr(skill, 'prompt_template', ''),
        "raw_content": raw_content,
    }


@app.put("/api/skills/{skill_name}")
def api_update_skill(skill_name: str, data: dict[str, Any]) -> dict[str, Any]:
    """Update skill SKILL.md file content."""
    skill = get_skill_by_name(skill_name)
    if skill is None:
        raise HTTPException(status_code=404, detail="Skill not found")
    
    skill_dir = getattr(skill, 'skill_dir', '')
    if not skill_dir:
        raise HTTPException(status_code=400, detail="Skill directory not found")
    
    skill_file = Path(skill_dir) / 'SKILL.md'
    if not skill_file.exists():
        raise HTTPException(status_code=404, detail="SKILL.md not found")
    
    new_content = data.get('content', '')
    skill_file.write_text(new_content)
    
    # Clear cache to reload skills
    import agents.skills as skills_module
    skills_module._cached_skills = None
    
    return {"status": "ok", "message": f"Skill {skill_name} updated"}


@app.get("/api/skills/stats")
def api_skill_stats() -> dict[str, Any]:
    """Get skill usage statistics."""
    return skill_stats()


@app.delete("/api/skills/{skill_name}")
def api_delete_skill(skill_name: str) -> dict[str, Any]:
    """Delete a skill (user and project-level skills can be deleted)."""
    skill = get_skill_by_name(skill_name)
    if skill is None:
        raise HTTPException(status_code=404, detail="Skill not found")
    
    skill_dir = getattr(skill, 'skill_dir', '')
    if not skill_dir:
        raise HTTPException(status_code=400, detail="Skill directory not found")
    
    import shutil
    try:
        shutil.rmtree(skill_dir)
        # Clear cache to reload skills
        import agents.skills as skills_module
        skills_module._cached_skills = None
        return {"status": "ok", "message": f"Skill {skill_name} deleted"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to delete skill: {str(e)}")


# ── Agents APIs ───────────────────────────────────────────────────────────────

@app.get("/api/agents")
def api_list_agents() -> list[dict[str, Any]]:
    """List all agents (built-in + custom) with their model configuration."""
    from agents.core.subagent import get_available_agent_types, _discover_custom_agents, get_agent_model_ref_env
    
    agents = []
    custom_agents = _discover_custom_agents()
    
    for agent_info in get_available_agent_types():
        name = agent_info["name"]
        description = agent_info["description"]
        
        # Get model configuration
        model_ref = get_agent_model_ref_env(name)
        
        # Check if it's a custom agent
        is_custom = name in custom_agents
        custom_config = custom_agents.get(name, {})
        
        agents.append({
            "name": name,
            "description": description,
            "model_ref": model_ref,
            "is_custom": is_custom,
            "allowed_tools": custom_config.get("allowed_tools"),
            "has_system_prompt": bool(custom_config.get("system_prompt")),
        })
    
    return agents


@app.get("/api/agents/{agent_name}")
def api_get_agent(agent_name: str) -> dict[str, Any]:
    """Get detailed agent configuration."""
    from agents.core.subagent import get_sub_agent_config, _discover_custom_agents
    
    custom_agents = _discover_custom_agents()
    is_custom = agent_name in custom_agents
    
    try:
        config = get_sub_agent_config(agent_name)
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"Agent not found: {e}")
    
    return {
        "name": agent_name,
        "is_custom": is_custom,
        "model_ref": config.get("model_ref", ""),
        "tools_count": len(config.get("tools", [])),
        "tools": [t["name"] for t in config.get("tools", [])],
        "system_prompt_preview": config.get("system_prompt", "")[:2000],
        "custom_config": custom_agents.get(agent_name, {}),
    }


@app.get("/api/endpoints")
def api_list_endpoints() -> list[dict[str, Any]]:
    """List all registered model endpoints from JSON config."""
    return list_endpoints()


@app.get("/api/endpoints/primary")
def api_get_primary_endpoint() -> dict[str, Any]:
    """Get the primary model endpoint configuration from JSON config."""
    return get_primary_endpoint()


# ── Config APIs ───────────────────────────────────────────────────────────────

class EndpointConfig(BaseModel):
    model: str
    base_url: str
    api_key: str


class ConfigUpdate(BaseModel):
    endpoints: dict[str, EndpointConfig]
    routing: dict[str, str]


class ModelVerifyRequest(BaseModel):
    base_url: str
    api_key: str
    model: str


@app.post("/api/models/verify")
async def api_verify_model(data: ModelVerifyRequest) -> dict[str, Any]:
    """Verify if a model endpoint is accessible and working."""
    import httpx
    
    try:
        # Use chat completions endpoint to verify (more universally supported)
        chat_url = f"{data.base_url.rstrip('/')}/chat/completions"
        headers = {
            "Authorization": f"Bearer {data.api_key}",
            "Content-Type": "application/json"
        }
        
        # Minimal test request
        test_payload = {
            "model": data.model,
            "messages": [{"role": "user", "content": "Hi"}],
            "max_tokens": 5,
            "stream": False
        }
        
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(chat_url, headers=headers, json=test_payload)
            
            if response.status_code == 200:
                return {
                    "status": "success",
                    "message": f"Model '{data.model}' is accessible and working",
                    "model_found": True
                }
            elif response.status_code == 401:
                return {
                    "status": "error",
                    "message": "Authentication failed - invalid API key"
                }
            elif response.status_code == 404:
                return {
                    "status": "error",
                    "message": f"Model '{data.model}' not found or endpoint not available"
                }
            else:
                # Try to parse error message
                try:
                    error_data = response.json()
                    error_msg = error_data.get('error', {}).get('message', response.text[:200])
                except:
                    error_msg = response.text[:200]
                
                return {
                    "status": "error",
                    "message": f"API returned HTTP {response.status_code}: {error_msg}"
                }
    
    except httpx.TimeoutException:
        return {
            "status": "error",
            "message": "Connection timeout - API endpoint not reachable"
        }
    except httpx.ConnectError as e:
        return {
            "status": "error",
            "message": f"Connection failed: {str(e)}"
        }
    except Exception as e:
        return {
            "status": "error",
            "message": f"Verification failed: {str(e)}"
        }


@app.get("/api/config")
def api_get_config() -> dict[str, Any]:
    """Get full application config from JSON file."""
    config = load_config()
    return config.to_dict()


@app.put("/api/config")
def api_update_config(data: ConfigUpdate) -> dict[str, Any]:
    """Update application config and save to JSON file."""
    config = AppConfig(
        endpoints={
            k: ModelEndpointConfig(
                model=v.model,
                base_url=v.base_url,
                api_key=v.api_key,
            )
            for k, v in data.endpoints.items()
        },
        routing=data.routing,
    )
    save_config(config)
    return {"status": "ok", "message": "Config saved successfully"}


# ── Skill Evolution APIs ──────────────────────────────────────────────────────

@app.get("/api/skill-evolution/report")
def api_skill_evolution_report() -> dict[str, Any]:
    """Get online skill evaluation report."""
    report_path = project_root / ".bear" / "skill-evolution" / "online_eval_report.json"
    if not report_path.exists():
        return {"status": "no_report"}
    try:
        report = json.loads(report_path.read_text())
        return {"status": "ok", "report": report}
    except Exception as e:
        return {"status": "error", "error": str(e)}


@app.get("/api/skill-evolution/provenance")
def api_skill_evolution_provenance() -> list[dict[str, Any]]:
    """Get skill evolution provenance log."""
    provenance_path = project_root / ".bear" / "skill-evolution" / "online_provenance.jsonl"
    if not provenance_path.exists():
        return []
    try:
        lines = provenance_path.read_text().splitlines()
        return [json.loads(line) for line in lines if line.strip()]
    except Exception:
        return []


@app.get("/api/skill-evolution/usage")
def api_skill_evolution_usage() -> dict[str, Any]:
    """Get skill usage statistics."""
    usage_path = project_root / ".bear" / "skill-evolution" / "skill_usage_stats.json"
    if not usage_path.exists():
        return {}
    try:
        return json.loads(usage_path.read_text())
    except Exception:
        return {}


# ── Trace APIs ────────────────────────────────────────────────────────────────

@app.get("/api/trace")
def api_trace_events(n: int = 50) -> dict[str, Any]:
    """Get recent trace events."""
    events = recent_events(n)
    return {
        "enabled": trace_enabled(),
        "path": str(trace_path()),
        "events": events,
    }


class TraceToggle(BaseModel):
    enabled: bool


@app.post("/api/trace/toggle")
def api_trace_toggle(data: TraceToggle) -> dict[str, bool]:
    """Enable or disable trace logging."""
    set_trace_enabled(data.enabled)
    return {"enabled": data.enabled}


# ── Workspace APIs ────────────────────────────────────────────────────────────

@app.get("/api/directories")
def api_list_directories(path: str | None = None) -> dict[str, Any]:
    """List directories for directory picker. If path is None, list home directory."""
    if path:
        target = Path(path).expanduser()
    else:
        target = Path.home()
    
    if not target.exists():
        raise HTTPException(status_code=404, detail="Path not found")
    if not target.is_dir():
        raise HTTPException(status_code=400, detail="Not a directory")
    
    dirs = []
    try:
        for item in sorted(target.iterdir()):
            if item.is_dir() and not item.name.startswith('.'):
                dirs.append({
                    "name": item.name,
                    "path": str(item),
                })
    except PermissionError:
        pass
    
    return {
        "current": str(target),
        "parent": str(target.parent) if target.parent != target else None,
        "directories": dirs,
    }


@app.get("/api/workspace/tree")
def api_workspace_tree(cwd: str | None = None) -> dict[str, Any]:
    """Get workspace file tree structure."""
    workspace_path = Path(cwd) if cwd else Path.cwd()
    if not workspace_path.exists():
        workspace_path = Path.cwd()
    
    def build_tree(path: Path, depth: int = 0) -> dict[str, Any] | None:
        if depth > 10:  # Prevent infinite recursion
            return None
        
        result = {
            "name": path.name,
            "path": str(path.relative_to(workspace_path)),
            "type": "directory" if path.is_dir() else "file",
        }
        
        if path.is_dir():
            # Skip hidden directories and common noise
            if path.name.startswith(".") or path.name in {"node_modules", "__pycache__", ".venv", ".venv-1", "Library", "Applications", ".Trash"}:
                return None
            children = []
            try:
                all_children = list(path.iterdir())
                # Sort: directories first, then files, each group alphabetically
                dirs = sorted([c for c in all_children if c.is_dir() and not (c.name.startswith(".") or c.name in {"node_modules", "__pycache__", ".venv", ".venv-1", "Library", "Applications", ".Trash"})], key=lambda p: p.name.lower())
                files = sorted([c for c in all_children if c.is_file()], key=lambda p: p.name.lower())
                for child in dirs + files:
                    child_tree = build_tree(child, depth + 1)
                    if child_tree:
                        children.append(child_tree)
            except (PermissionError, InterruptedError, OSError):
                # Skip directories we can't access
                pass
            result["children"] = children
        else:
            # For files, include size
            try:
                result["size"] = path.stat().st_size
            except Exception:
                result["size"] = 0
        
        return result
    
    tree = build_tree(workspace_path)
    return tree or {"name": workspace_path.name, "path": ".", "type": "directory", "children": []}


@app.delete("/api/workspace/file")
def api_workspace_delete_file(path: str, cwd: str | None = None) -> dict[str, Any]:
    """Delete a file or directory."""
    import shutil
    workspace_path = Path(cwd) if cwd else Path.cwd()
    file_path = workspace_path / path
    
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File not found")
    
    # Security check: ensure path is within workspace
    try:
        file_path.resolve().relative_to(workspace_path.resolve())
    except ValueError:
        raise HTTPException(status_code=403, detail="Access denied")
    
    try:
        if file_path.is_dir():
            shutil.rmtree(file_path)
        else:
            file_path.unlink()
        return {"success": True, "message": f"Deleted {path}"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to delete: {str(e)}")


class CreateFileRequest(BaseModel):
    path: str
    cwd: str | None = None


@app.post("/api/workspace/create")
def api_workspace_create_file(data: CreateFileRequest) -> dict[str, Any]:
    """Create a new file (and parent directories if needed)."""
    workspace_path = Path(data.cwd) if data.cwd else Path.cwd()
    file_path = workspace_path / data.path

    if not data.path or not data.path.strip():
        raise HTTPException(status_code=400, detail="File name cannot be empty")

    # Security check: ensure path is within workspace
    try:
        file_path.resolve().relative_to(workspace_path.resolve())
    except ValueError:
        raise HTTPException(status_code=403, detail="Access denied")

    if file_path.exists():
        raise HTTPException(status_code=400, detail="File already exists")

    try:
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.touch()
        return {"success": True, "path": str(file_path.relative_to(workspace_path))}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to create file: {str(e)}")


class RenameRequest(BaseModel):
    old_path: str
    new_name: str
    cwd: str | None = None


@app.post("/api/workspace/rename")
def api_workspace_rename(data: RenameRequest) -> dict[str, Any]:
    """Rename a file or directory."""
    workspace_path = Path(data.cwd) if data.cwd else Path.cwd()
    old_path = workspace_path / data.old_path
    
    if not old_path.exists():
        raise HTTPException(status_code=404, detail="File not found")
    
    # Security check: ensure path is within workspace
    try:
        old_path.resolve().relative_to(workspace_path.resolve())
    except ValueError:
        raise HTTPException(status_code=403, detail="Access denied")
    
    new_path = old_path.parent / data.new_name
    
    if new_path.exists():
        raise HTTPException(status_code=400, detail="Target already exists")
    
    try:
        old_path.rename(new_path)
        return {"success": True, "old_path": data.old_path, "new_path": str(new_path.relative_to(workspace_path))}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to rename: {str(e)}")


@app.get("/api/workspace/file")
def api_workspace_file(path: str) -> dict[str, Any]:
    """Get file content."""
    file_path = Path.cwd() / path
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File not found")
    if not file_path.is_file():
        raise HTTPException(status_code=400, detail="Not a file")
    
    try:
        content = file_path.read_text(encoding="utf-8")
        return {
            "path": path,
            "content": content,
            "size": file_path.stat().st_size,
        }
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="Binary file")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── MCP Server APIs ───────────────────────────────────────────────────────────

@app.get("/api/mcp")
def api_list_mcp_servers() -> list[dict[str, Any]]:
    """List configured MCP servers from .mcp.json."""
    mcp_config_path = project_root / ".mcp.json"
    if not mcp_config_path.exists():
        return []
    try:
        config = json.loads(mcp_config_path.read_text())
        servers = config.get("mcpServers", {})
        return [
            {
                "name": name,
                "command": server.get("command", ""),
                "args": server.get("args", []),
                "env": server.get("env", {}),
            }
            for name, server in servers.items()
        ]
    except Exception:
        return []


@app.get("/api/mcp/tools")
async def api_list_mcp_tools() -> list[dict[str, Any]]:
    """Connect to all MCP servers and return their tools."""
    import asyncio
    from agents.model.mcp_client import McpManager

    manager = McpManager()
    try:
        await manager.load_and_connect()
        tools = manager.get_tool_definitions()
        # Group tools by server
        server_tools: dict[str, list[dict]] = {}
        for t in tools:
            # name format: mcp__serverName__toolName
            parts = t["name"].split("__", 2)
            server_name = parts[1] if len(parts) >= 2 else "unknown"
            tool_name = parts[2] if len(parts) >= 3 else t["name"]
            if server_name not in server_tools:
                server_tools[server_name] = []
            server_tools[server_name].append({
                "name": tool_name,
                "full_name": t["name"],
                "description": t.get("description", ""),
                "input_schema": t.get("input_schema", {}),
            })
        return [
            {"server": name, "tools": tlist, "tool_count": len(tlist)}
            for name, tlist in server_tools.items()
        ]
    except Exception as e:
        return [{"error": str(e)}]
    finally:
        await manager.disconnect_all()


@app.get("/api/tools/native")
def api_list_native_tools() -> list[dict[str, Any]]:
    """List native tools defined in agents/tools.py."""
    from agents.tools import tool_definitions
    return [
        {
            "name": t["name"],
            "description": t.get("description", ""),
            "input_schema": t.get("input_schema", {}),
        }
        for t in tool_definitions
    ]


# ── Chat APIs ─────────────────────────────────────────────────────────────────

class ChatMessage(BaseModel):
    message: str
    session_id: str | None = None
    context_files: list[str] | None = None
    agent: str | None = None
    model: str | None = None
    permission_mode: str | None = None  # "default", "bypassPermissions", "plan"


@app.post("/api/chat")
async def api_chat(data: ChatMessage) -> dict[str, Any]:
    """
    Send a chat message and get response from the agent.
    """
    try:
        from agents.agent import Agent
        from agents.config import load_config
        
        # Load config to get API credentials
        config = load_config()
        
        # If no model specified or model not found, use first available endpoint
        model_name = data.model
        api_key = None
        api_base = None
        
        if model_name:
            # Try to find the specified model
            for endpoint in config.endpoints.values():
                if endpoint.model == model_name:
                    api_key = endpoint.api_key
                    api_base = endpoint.base_url
                    break
        
        # If model not found or not specified, use first endpoint
        if not api_key and config.endpoints:
            first_endpoint = next(iter(config.endpoints.values()))
            model_name = first_endpoint.model
            api_key = first_endpoint.api_key
            api_base = first_endpoint.base_url
        
        # Create agent instance with credentials
        agent = Agent(
            model=model_name,
            api_key=api_key,
            api_base=api_base,
        )
        
        # Build context from files and directories
        context = ""
        if data.context_files:
            for item_path in data.context_files:
                try:
                    full_path = project_root / item_path
                    if full_path.exists():
                        if full_path.is_dir():
                            # For directories, list first-level structure
                            structure = f"\n\n--- {item_path}/ (directory structure) ---\n"
                            try:
                                entries = sorted(full_path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
                                for entry in entries[:50]:  # Limit to 50 entries
                                    prefix = "📁 " if entry.is_dir() else "📄 "
                                    structure += f"{prefix}{entry.name}\n"
                                if len(entries) > 50:
                                    structure += f"... and {len(entries) - 50} more entries\n"
                            except PermissionError:
                                structure += "(Permission denied)\n"
                            context += structure
                        else:
                            # For files, read content
                            content = full_path.read_text(encoding="utf-8")
                            context += f"\n\n--- {item_path} ---\n{content}"
                except Exception as e:
                    context += f"\n\n--- {item_path} ---\nError reading: {e}"
        
        # Prepare message with context
        full_message = data.message
        if context:
            full_message = f"{data.message}\n\nContext files:{context}"
        
        # Process the message
        await agent.chat(full_message)
        
        # Get the response
        from agents.service import AgentService
        response = AgentService(agent).last_response or ''
        
        return {
            "response": response,
            "session_id": agent.session_id,
        }
    except Exception as e:
        import traceback
        return {
            "response": f"Error processing message: {str(e)}\n\n{traceback.format_exc()}",
            "session_id": data.session_id or "error-session",
        }


@app.post("/api/chat/stream")
async def api_chat_stream(data: ChatMessage):
    """
    Send a chat message and get streaming response via SSE.
    """
    try:
        from agents.agent import Agent
        from agents.config import load_config
        
        # Load config to get API credentials
        config = load_config()
        
        # If no model specified or model not found, use first available endpoint
        model_name = data.model
        api_key = None
        api_base = None
        
        if model_name:
            # Try to find the specified model
            for endpoint in config.endpoints.values():
                if endpoint.model == model_name:
                    api_key = endpoint.api_key
                    api_base = endpoint.base_url
                    break
        
        # If model not found or not specified, use first endpoint
        if not api_key and config.endpoints:
            first_endpoint = next(iter(config.endpoints.values()))
            model_name = first_endpoint.model
            api_key = first_endpoint.api_key
            api_base = first_endpoint.base_url
        
        # Create agent instance with credentials
        agent = Agent(
            model=model_name,
            api_key=api_key,
            api_base=api_base,
            permission_mode=data.permission_mode or "bypassPermissions",  # Web 模式默认 YOLO
        )
        
        # Restore session if session_id is provided
        if data.session_id:
            from agents.core.session import load_session
            from agents.service import AgentService
            session_data = load_session(data.session_id)
            if session_data:
                svc = AgentService(agent)
                svc.restore(session_data)
                agent.session_id = data.session_id
        
        # Build context from files and directories
        context = ""
        if data.context_files:
            for item_path in data.context_files:
                try:
                    full_path = project_root / item_path
                    if full_path.exists():
                        if full_path.is_dir():
                            # For directories, list first-level structure
                            structure = f"\n\n--- {item_path}/ (directory structure) ---\n"
                            try:
                                entries = sorted(full_path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
                                for entry in entries[:50]:  # Limit to 50 entries
                                    prefix = "📁 " if entry.is_dir() else "📄 "
                                    structure += f"{prefix}{entry.name}\n"
                                if len(entries) > 50:
                                    structure += f"... and {len(entries) - 50} more entries\n"
                            except PermissionError:
                                structure += "(Permission denied)\n"
                            context += structure
                        else:
                            # For files, read content
                            content = full_path.read_text(encoding="utf-8")
                            context += f"\n\n--- {item_path} ---\n{content}"
                except Exception as e:
                    context += f"\n\n--- {item_path} ---\nError reading: {e}"
        
        # Prepare message with context
        full_message = data.message
        if context:
            full_message = f"{data.message}\n\nContext files:{context}"
        
        async def generate():
            # 使用新的事件流机制
            current_sub_agent_id = None
            
            # Register agent in active sessions for control endpoints
            session_id = agent.session_id or data.session_id or "unknown"
            _active_sessions[session_id] = {"agent": agent}
            
            try:
                async for event in agent.chat_stream(full_message):
                    event_type = event.get("type")
                    # 从事件中获取 sub_agent_id（如果有的话）
                    event_sub_agent_id = event.get("sub_agent_id")
                    
                    if event_type == "thinking":
                        data = {'thinking': {'content': event.get('content', '')}}
                        sub_id = event_sub_agent_id or current_sub_agent_id
                        if sub_id:
                            data['thinking']['sub_agent_id'] = sub_id
                        yield f"data: {json.dumps(data)}\n\n"
                    elif event_type == "text":
                        data = {'text': {'content': event.get('content', '')}}
                        sub_id = event_sub_agent_id or current_sub_agent_id
                        if sub_id:
                            data['text']['sub_agent_id'] = sub_id
                        yield f"data: {json.dumps(data)}\n\n"
                    elif event_type == "tool_call":
                        call_id = event.get('call_id', f"call_{id(event)}")
                        data = {'tool_call': {
                            'call_id': call_id,
                            'name': event.get('name', ''), 
                            'input': event.get('input', {})
                        }}
                        sub_id = event_sub_agent_id or current_sub_agent_id
                        if sub_id:
                            data['tool_call']['sub_agent_id'] = sub_id
                        yield f"data: {json.dumps(data)}\n\n"
                    elif event_type == "tool_result":
                        call_id = event.get('call_id', f"call_{id(event)}")
                        data = {'tool_result': {
                            'call_id': call_id,
                            'name': event.get('name', ''),
                            'result': event.get('result', ''),
                            'status': event.get('status', 'ok'),
                        }}
                        if event.get('snapshot'):
                            data['tool_result']['snapshot'] = event['snapshot']
                        if event.get('duration_ms') is not None:
                            data['tool_result']['duration_ms'] = event['duration_ms']
                        sub_id = event_sub_agent_id or current_sub_agent_id
                        if sub_id:
                            data['tool_result']['sub_agent_id'] = sub_id
                        yield f"data: {json.dumps(data)}\n\n"
                    elif event_type == "sub_agent_start":
                        # 使用事件中传来的 agent_id
                        current_sub_agent_id = event.get('agent_id')
                        sa_start = {'agent_type': event.get('agent_type', ''), 'description': event.get('description', ''), 'agent_id': current_sub_agent_id}
                        yield f"data: {json.dumps({'sub_agent_start': sa_start})}\n\n"
                    elif event_type == "sub_agent_end":
                        sa_end = {'agent_type': event.get('agent_type', ''), 'description': event.get('description', ''), 'agent_id': current_sub_agent_id}
                        yield f"data: {json.dumps({'sub_agent_end': sa_end})}\n\n"
                        current_sub_agent_id = None
                    elif event_type == "info":
                        yield f"data: {json.dumps({'info': event.get('message', '')})}\n\n"
                    elif event_type == "error":
                        yield f"data: {json.dumps({'error': {'message': event.get('message', '')}})}\n\n"
                    elif event_type == "permission_request":
                        # 权限请求事件
                        perm_request = {
                            'permission_request': {
                                'request_id': event.get('request_id', ''),
                                'command': event.get('command', ''),
                                'tool_name': event.get('tool_name', ''),
                            }
                        }
                        sub_id = event_sub_agent_id or current_sub_agent_id
                        if sub_id:
                            perm_request['permission_request']['sub_agent_id'] = sub_id
                        yield f"data: {json.dumps(perm_request)}\n\n"
            finally:
                # 确保session被保存
                from agents.service import AgentService
                await AgentService(agent).save()
                # Unregister from active sessions
                _active_sessions.pop(session_id, None)
            
            # Send completion event
            yield f"data: {json.dumps({'done': True, 'session_id': agent.session_id})}\n\n"
        
        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",  # Disable nginx buffering
            }
        )
    except Exception as e:
        import traceback
        error_msg = f"Error processing message: {str(e)}\n\n{traceback.format_exc()}"
        return JSONResponse(
            status_code=500,
            content={"error": error_msg, "session_id": data.session_id or "error-session"}
        )


# ── Health Check ──────────────────────────────────────────────────────────────

@app.get("/api/health")
def api_health() -> dict[str, str]:
    """Health check endpoint."""
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=5555)
