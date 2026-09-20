"""Session management APIs."""

from __future__ import annotations

import time
import copy
import uuid
import json
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from agents.core.workspace import workspace_scope
from agents.core.session import list_sessions, delete_session, session_dir

router = APIRouter(tags=["sessions"])

_active_sessions: dict[str, dict] = {}


def get_active_sessions() -> dict[str, dict]:
    return _active_sessions


class FrontendLogRequest(BaseModel):
    message: str
    level: str = "info"


@router.post("/api/frontend-log")
def api_frontend_log(data: FrontendLogRequest) -> dict[str, Any]:
    """接收前端日志并保存到文件"""
    log_dir = Path.home() / ".mycode" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "frontend.log"
    
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    log_entry = f"[{timestamp}] [{data.level.upper()}] {data.message}\n"
    
    with open(log_file, "a", encoding="utf-8") as f:
        f.write(log_entry)
    
    return {"success": True}


class SteerRequest(BaseModel):
    message: str


class RewindRequest(BaseModel):
    turns: int = 1


class RewindStageRequest(BaseModel):
    """统一回退 stage：turns 与 keep_user_messages 二选一。"""
    turns: Optional[int] = None
    keep_user_messages: Optional[int] = None


class RewindCommitRequest(BaseModel):
    plan_id: str


class RewindClearRequest(BaseModel):
    plan_id: str


class ForkRequest(BaseModel):
    """原子 Fork 请求：从指定 seq 切割，创建新 session"""
    at_seq: Optional[int] = None  # 切割点，None 表示完整复制
    keep_user_messages: Optional[int] = None  # 保留的用户消息数（向后兼容）


class PermissionModeRequest(BaseModel):
    mode: str


@router.get("/api/sessions")
def api_list_sessions() -> list[dict[str, Any]]:
    sessions = list_sessions()
    # 统一 startTime 为字符串用于排序
    def sort_key(s):
        start_time = s.get("startTime", "")
        # 如果是整数（时间戳），转换为字符串
        if isinstance(start_time, int):
            return str(start_time)
        return str(start_time) if start_time else ""
    sessions.sort(key=sort_key, reverse=True)
    print(f"[LIST] sessions: {len(sessions)}")
    for s in sessions[:5]:
        print(f"[LIST]   {s.get('id')}: name={s.get('name', 'N/A')}, cwd={s.get('cwd', 'N/A')[:30]}")
    return sessions


@router.get("/api/sessions/{session_id}")
def api_get_session(session_id: str) -> dict[str, Any]:
    print(f"[GET] session_id={session_id}")
    from agents.core.session import Session
    session = Session.load_from_events(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    
    events = session.events
    metadata = {
        "id": session.id,
        "name": session.title or session.id,
        "cwd": session.projections.get("cwd", ""),
    }
    
    print(f"[GET] result: name={metadata.get('name')}, events={len(events)}, projections={session.projections}")
    return {
        "metadata": metadata,
        "events": events,
        "projections": session.projections,
    }


@router.put("/api/sessions/{session_id}")
def api_update_session(session_id: str, request: dict[str, Any]) -> dict[str, Any]:
    """重命名 session：追加 session/title 事件（单一数据源=事件日志，自动持久化+投影+WS广播）。"""
    if "name" not in request:
        raise HTTPException(status_code=400, detail="Only 'name' updates are supported")
    
    from agents.session_manager import get_session_manager
    from agents.core.session import Session
    
    session = get_session_manager().get(session_id)
    if session is None:
        session = Session.load_from_events(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    
    session.append("session/title", {"title": request["name"]})
    print(f"[PUT] Renamed session {session_id} -> {request['name']}")
    return {"success": True, "message": "Session renamed"}


@router.get("/api/sessions/{session_id}/projections")
def api_get_session_projections(session_id: str) -> dict[str, Any]:
    """快速获取 session 投影值（title, updatedAt, cwd, running）。"""
    try:
        from agents.core.session import Session
        session = Session.load_from_events(session_id)
        if session:
            return session.projections
    except Exception:
        pass
    
    raise HTTPException(status_code=404, detail="Session not found")


@router.get("/api/sessions/{session_id}/summary")
def api_session_summary(session_id: str) -> dict[str, Any]:
    session_info = _active_sessions.get(session_id)

    result: dict[str, Any] = {}

    if session_info and session_info.get("svc"):
        svc = session_info["svc"]
        agent = getattr(svc, '_agent', None) or svc.agent
        session = getattr(svc, 'session', None) or getattr(agent, 'session', None)

        latest_stats: dict[str, Any] = {}
        if session:
            for event in reversed(session._log):
                if event.get("type") == "stats":
                    latest_stats = event
                    break

        stats = svc.get_stats()
        cached_tokens = getattr(agent, 'total_cached_tokens', 0)
        result["stats"] = {
            "input_tokens": stats.get("input", 0),
            "output_tokens": stats.get("output", 0),
            "cached_tokens": cached_tokens,
            "context_window": agent.context_window,
            "effective_window": agent.effective_window,
            "last_input_token_count": agent.last_input_token_count,
        }

        result["metadata"] = {
            "id": session_id,
            "name": session.projections.get("title", session_id) if session else session_id,
            "cwd": session.projections.get("cwd", "") if session else "",
        }
        result["projections"] = session.projections if session else {}

        perm_mode = getattr(svc, 'permission_mode', 'default')
        result["permission_mode"] = perm_mode

        has_breakdown = latest_stats.get("system_chars", 0) > 0 or latest_stats.get("user_chars", 0) > 0
        if has_breakdown:
            result["breakdown"] = _compute_breakdown_from_stats(latest_stats, agent)
        else:
            result["breakdown"] = _compute_breakdown_from_agent(agent)
    else:
        sessions_dir = session_dir()
        events_file = sessions_dir / f"{session_id}.events.jsonl"
        if not events_file.exists():
            raise HTTPException(status_code=404, detail="Session not found")

        from agents.core.session import Session
        session = Session.load_from_events(session_id)
        if session:
            result["metadata"] = {
                "id": session_id,
                "name": session.title or session_id,
                "cwd": session.projections.get("cwd", ""),
            }
            result["projections"] = session.projections
        else:
            result["metadata"] = {"id": session_id, "name": session_id, "cwd": ""}
            result["projections"] = {}
        result["permission_mode"] = "default"

        latest_stats = {}
        total_cached_tokens = 0
        with open(events_file) as f:
            for line in f:
                try:
                    event = json.loads(line.strip())
                    if event.get("type") == "stats":
                        latest_stats = event
                        total_cached_tokens += event.get("cached_tokens", 0)
                except (json.JSONDecodeError, Exception):
                    continue

        result["stats"] = {
            "input_tokens": latest_stats.get("input_tokens", 0),
            "output_tokens": latest_stats.get("output_tokens", 0),
            "cached_tokens": total_cached_tokens,
            "context_window": latest_stats.get("context_window", 128000),
            "effective_window": 108000,
            "last_input_token_count": latest_stats.get("last_input_token_count", 0),
        }

        if latest_stats and latest_stats.get("system_chars", 0) > 0:
            result["breakdown"] = _compute_breakdown_from_stats(latest_stats, None)
        else:
            result["breakdown"] = _compute_breakdown_from_jsonl(events_file, latest_stats)

    return result


def _compute_breakdown_from_stats(latest_stats: dict, agent: Any) -> dict[str, Any]:
    actual_input_tokens = latest_stats.get("last_input_token_count", 0)
    system_chars = latest_stats.get("system_chars", 0)
    user_chars = latest_stats.get("user_chars", 0)
    assistant_chars = latest_stats.get("assistant_chars", 0)
    tool_result_chars = latest_stats.get("tool_result_chars", 0)

    system_base_chars = latest_stats.get("system_base_chars", 0)
    system_claude_md_chars = latest_stats.get("system_claude_md_chars", 0)
    system_skills_chars = latest_stats.get("system_skills_chars", 0)
    system_memory_chars = latest_stats.get("system_memory_chars", 0)
    system_wiki_chars = latest_stats.get("system_wiki_chars", 0)
    system_agents_chars = latest_stats.get("system_agents_chars", 0)
    system_workspace_chars = latest_stats.get("system_workspace_chars", 0)

    total_chars = system_chars + user_chars + assistant_chars + tool_result_chars

    builtin_tool_count = 0
    mcp_tool_count = 0
    if agent:
        from agents.tools.registry import get_active_tool_definitions
        tools = getattr(agent, 'tools', [])
        tool_defs = get_active_tool_definitions(tools)
        mcp_tool_count = sum(1 for t in tool_defs if t.get('name', '').startswith('mcp__'))
        builtin_tool_count = len(tool_defs) - mcp_tool_count

    if actual_input_tokens > 0 and total_chars > 0:
        scale = actual_input_tokens / (total_chars / 4)
        user_tokens = int((user_chars / 4) * scale)
        assistant_tokens = int((assistant_chars / 4) * scale)
        tool_tokens = int((tool_result_chars / 4) * scale)
        base_prompt_tokens = int(((system_base_chars + system_workspace_chars) / 4) * scale)
        claude_md_tokens = int((system_claude_md_chars / 4) * scale)
        skills_tokens = int((system_skills_chars / 4) * scale)
        memory_tokens = int((system_memory_chars / 4) * scale)
        wiki_tokens = int((system_wiki_chars / 4) * scale)
        agents_tokens = int((system_agents_chars / 4) * scale)
        plan_mode_chars = latest_stats.get("plan_mode_chars", 0)
        plan_mode_tokens = int((plan_mode_chars / 4) * scale)
    else:
        user_tokens = user_chars // 4
        assistant_tokens = assistant_chars // 4
        tool_tokens = tool_result_chars // 4
        base_prompt_tokens = (system_base_chars + system_workspace_chars) // 4
        claude_md_tokens = system_claude_md_chars // 4
        skills_tokens = system_skills_chars // 4
        memory_tokens = system_memory_chars // 4
        wiki_tokens = system_wiki_chars // 4
        agents_tokens = system_agents_chars // 4
        plan_mode_tokens = latest_stats.get("plan_mode_chars", 0) // 4

    messages_tokens = user_tokens + assistant_tokens + tool_tokens

    tool_result_by_name_chars = latest_stats.get("tool_result_by_name", {})
    tool_result_by_name = {}
    if tool_result_by_name_chars and tool_tokens > 0:
        total_tool_chars = sum(tool_result_by_name_chars.values())
        if total_tool_chars > 0:
            for tool_name, chars in tool_result_by_name_chars.items():
                tool_result_by_name[tool_name] = int(tool_tokens * (chars / total_tool_chars))

    return {
        "base_prompt_tokens": base_prompt_tokens,
        "claude_md_tokens": claude_md_tokens,
        "skills_tokens": skills_tokens,
        "memory_tokens": memory_tokens,
        "wiki_tokens": wiki_tokens,
        "agents_tokens": agents_tokens,
        "tools_tokens": tool_tokens,
        "builtin_tool_count": builtin_tool_count,
        "mcp_tool_count": mcp_tool_count,
        "messages_tokens": messages_tokens,
        "message_count": latest_stats.get("msg_count", 0),
        "user_tokens": user_tokens,
        "assistant_tokens": assistant_tokens,
        "tool_tokens": tool_tokens,
        "tool_result_by_name": tool_result_by_name,
        "total_tokens": actual_input_tokens,
        "is_plan_mode": latest_stats.get("is_plan_mode", False),
        "plan_mode_tokens": plan_mode_tokens,
    }


def _compute_breakdown_from_agent(agent: Any) -> dict[str, Any]:
    messages = agent.messages or []
    system_chars = 0
    user_chars = 0
    assistant_chars = 0
    tool_result_chars = 0
    message_count = 0

    for msg in messages:
        role = msg.get('role', '')
        content = msg.get('content', '')
        if isinstance(content, str):
            chars = len(content)
        elif isinstance(content, list):
            chars = sum(len(item.get('text', '')) for item in content if isinstance(item, dict) and item.get('type') == 'text')
        else:
            chars = 0
        if role == 'system':
            system_chars = chars
        elif role == 'user':
            user_chars += chars
            message_count += 1
        elif role == 'assistant':
            assistant_chars += chars
            message_count += 1
        elif role == 'tool':
            tool_result_chars += chars
            message_count += 1

    from agents.tools.registry import get_active_tool_definitions
    tools = getattr(agent, 'tools', [])
    tool_defs = get_active_tool_definitions(tools)
    tools_json = json.dumps([{
        'name': t.get('name', ''),
        'description': t.get('description', ''),
        'parameters': t.get('parameters', {}),
    } for t in tool_defs], ensure_ascii=False)
    tools_chars = len(tools_json)
    mcp_tool_count = sum(1 for t in tool_defs if t.get('name', '').startswith('mcp__'))
    builtin_tool_count = len(tool_defs) - mcp_tool_count

    try:
        from agents.core.prompt import (
            load_claude_md, build_skill_descriptions,
            build_memory_prompt_section, build_wiki_prompt_section,
            build_agent_descriptions, build_workspace_structure
        )
        system_claude_md_chars = len(load_claude_md())
        system_skills_chars = len(build_skill_descriptions())
        system_memory_chars = len(build_memory_prompt_section())
        system_wiki_chars = len(build_wiki_prompt_section())
        system_agents_chars = len(build_agent_descriptions())
        system_workspace_chars = len(build_workspace_structure())
        system_base_chars = system_chars - system_claude_md_chars - system_skills_chars - system_memory_chars - system_wiki_chars - system_agents_chars - system_workspace_chars
    except Exception:
        system_base_chars = system_chars
        system_claude_md_chars = 0
        system_skills_chars = 0
        system_memory_chars = 0
        system_wiki_chars = 0
        system_agents_chars = 0
        system_workspace_chars = 0

    is_plan_mode = getattr(agent, 'permission_mode', '') == 'plan'
    plan_mode_chars = 0
    if is_plan_mode and hasattr(agent, '_plan_mode_manager') and agent._plan_mode_manager:
        try:
            plan_mode_chars = len(agent._plan_mode_manager.build_plan_mode_prompt())
        except Exception:
            pass

    actual_input_tokens = getattr(agent, 'last_input_token_count', 0)
    total_chars = system_chars + tools_chars + user_chars + assistant_chars + tool_result_chars

    if actual_input_tokens > 0 and total_chars > 0:
        scale = actual_input_tokens / (total_chars / 4)
        base_prompt_tokens = int(((system_base_chars + system_workspace_chars) / 4) * scale)
        claude_md_tokens = int((system_claude_md_chars / 4) * scale)
        skills_tokens = int((system_skills_chars / 4) * scale)
        memory_tokens = int((system_memory_chars / 4) * scale)
        wiki_tokens = int((system_wiki_chars / 4) * scale)
        agents_tokens = int((system_agents_chars / 4) * scale)
        tools_tokens = int((tools_chars / 4) * scale)
        user_tokens = int((user_chars / 4) * scale)
        assistant_tokens = int((assistant_chars / 4) * scale)
        tool_tokens = int((tool_result_chars / 4) * scale)
        plan_mode_tokens = int((plan_mode_chars / 4) * scale)
    else:
        base_prompt_tokens = (system_base_chars + system_workspace_chars) // 4
        claude_md_tokens = system_claude_md_chars // 4
        skills_tokens = system_skills_chars // 4
        memory_tokens = system_memory_chars // 4
        wiki_tokens = system_wiki_chars // 4
        agents_tokens = system_agents_chars // 4
        tools_tokens = tools_chars // 4
        user_tokens = user_chars // 4
        assistant_tokens = assistant_chars // 4
        tool_tokens = tool_result_chars // 4
        plan_mode_tokens = plan_mode_chars // 4

    messages_tokens = user_tokens + assistant_tokens + tool_tokens

    tool_result_by_name_chars = getattr(agent, '_tool_result_chars', {})
    tool_result_by_name = {}
    if tool_result_by_name_chars and tool_tokens > 0:
        total_tool_chars = sum(tool_result_by_name_chars.values())
        if total_tool_chars > 0:
            for tool_name, chars in tool_result_by_name_chars.items():
                tool_result_by_name[tool_name] = int(tool_tokens * (chars / total_tool_chars))

    return {
        "base_prompt_tokens": base_prompt_tokens,
        "claude_md_tokens": claude_md_tokens,
        "skills_tokens": skills_tokens,
        "memory_tokens": memory_tokens,
        "wiki_tokens": wiki_tokens,
        "agents_tokens": agents_tokens,
        "tools_tokens": tools_tokens,
        "builtin_tool_count": builtin_tool_count,
        "mcp_tool_count": mcp_tool_count,
        "messages_tokens": messages_tokens,
        "message_count": message_count,
        "user_tokens": user_tokens,
        "assistant_tokens": assistant_tokens,
        "tool_tokens": tool_tokens,
        "tool_result_by_name": tool_result_by_name,
        "total_tokens": actual_input_tokens if actual_input_tokens > 0 else total_chars // 4,
        "is_plan_mode": is_plan_mode,
        "plan_mode_tokens": plan_mode_tokens,
    }


def _compute_breakdown_from_jsonl(events_file: Path, latest_stats: dict) -> dict[str, Any]:
    if not latest_stats:
        return {
            "base_prompt_tokens": 0, "claude_md_tokens": 0, "skills_tokens": 0,
            "memory_tokens": 0, "wiki_tokens": 0, "agents_tokens": 0,
            "tools_tokens": 0, "builtin_tool_count": 0, "mcp_tool_count": 0,
            "messages_tokens": 0, "message_count": 0,
            "user_tokens": 0, "assistant_tokens": 0, "tool_tokens": 0,
            "tool_result_by_name": {},
            "total_tokens": 0, "is_plan_mode": False, "plan_mode_tokens": 0,
        }

    actual_input_tokens = latest_stats.get("last_input_token_count", 0)
    system_chars = latest_stats.get("system_chars", 0)
    user_chars = latest_stats.get("user_chars", 0)
    assistant_chars = latest_stats.get("assistant_chars", 0)
    tool_result_chars = latest_stats.get("tool_result_chars", 0)

    system_base_chars = latest_stats.get("system_base_chars", 0)
    system_claude_md_chars = latest_stats.get("system_claude_md_chars", 0)
    system_skills_chars = latest_stats.get("system_skills_chars", 0)
    system_memory_chars = latest_stats.get("system_memory_chars", 0)
    system_wiki_chars = latest_stats.get("system_wiki_chars", 0)
    system_agents_chars = latest_stats.get("system_agents_chars", 0)
    system_workspace_chars = latest_stats.get("system_workspace_chars", 0)
    message_count = latest_stats.get("msg_count", 0)

    if system_chars == 0 and user_chars == 0:
        system_chars = 6000
        system_base_chars = int(system_chars * 0.5)
        system_claude_md_chars = int(system_chars * 0.2)
        system_skills_chars = int(system_chars * 0.1)
        system_memory_chars = int(system_chars * 0.05)
        system_wiki_chars = int(system_chars * 0.05)
        system_agents_chars = int(system_chars * 0.05)
        system_workspace_chars = int(system_chars * 0.05)

    try:
        from frontend.server.mcp_manager import global_mcp_manager
        tool_defs = global_mcp_manager.get_tool_definitions()
        tools_json = json.dumps([{
            'name': t.get('name', ''),
            'description': t.get('description', ''),
            'parameters': t.get('parameters', {}),
        } for t in tool_defs], ensure_ascii=False)
        tools_chars = len(tools_json)
        mcp_tool_count = len(tool_defs)
        builtin_tool_count = 0
    except Exception:
        tools_chars = 0
        builtin_tool_count = latest_stats.get("tool_count", 0)
        mcp_tool_count = 0

    total_chars = system_chars + tools_chars + user_chars + assistant_chars + tool_result_chars

    if actual_input_tokens > 0 and total_chars > 0:
        scale = actual_input_tokens / (total_chars / 4)
        user_tokens = int((user_chars / 4) * scale)
        assistant_tokens = int((assistant_chars / 4) * scale)
        tool_tokens = int((tool_result_chars / 4) * scale)
        tools_definition_tokens = int((tools_chars / 4) * scale)
        base_prompt_tokens = int(((system_base_chars + system_workspace_chars) / 4) * scale)
        claude_md_tokens = int((system_claude_md_chars / 4) * scale)
        skills_tokens = int((system_skills_chars / 4) * scale)
        memory_tokens = int((system_memory_chars / 4) * scale)
        wiki_tokens = int((system_wiki_chars / 4) * scale)
        agents_tokens = int((system_agents_chars / 4) * scale)
    else:
        user_tokens = user_chars // 4
        assistant_tokens = assistant_chars // 4
        tool_tokens = tool_result_chars // 4
        tools_definition_tokens = tools_chars // 4
        base_prompt_tokens = (system_base_chars + system_workspace_chars) // 4
        claude_md_tokens = system_claude_md_chars // 4
        skills_tokens = system_skills_chars // 4
        memory_tokens = system_memory_chars // 4
        wiki_tokens = system_wiki_chars // 4
        agents_tokens = system_agents_chars // 4

    messages_tokens = user_tokens + assistant_tokens + tool_tokens

    is_plan_mode = latest_stats.get("is_plan_mode", False)
    plan_mode_chars = latest_stats.get("plan_mode_chars", 0)
    plan_mode_tokens = int((plan_mode_chars / 4) * scale) if actual_input_tokens > 0 and total_chars > 0 else plan_mode_chars // 4

    tool_result_by_name_chars = latest_stats.get("tool_result_by_name", {})
    tool_result_by_name = {}
    if tool_result_by_name_chars and tool_tokens > 0:
        total_tool_chars = sum(tool_result_by_name_chars.values())
        if total_tool_chars > 0:
            for tool_name, chars in tool_result_by_name_chars.items():
                tool_result_by_name[tool_name] = int(tool_tokens * (chars / total_tool_chars))

    return {
        "base_prompt_tokens": base_prompt_tokens,
        "claude_md_tokens": claude_md_tokens,
        "skills_tokens": skills_tokens,
        "memory_tokens": memory_tokens,
        "wiki_tokens": wiki_tokens,
        "agents_tokens": agents_tokens,
        "tools_tokens": tools_definition_tokens,
        "builtin_tool_count": builtin_tool_count,
        "mcp_tool_count": mcp_tool_count,
        "messages_tokens": messages_tokens,
        "message_count": message_count,
        "user_tokens": user_tokens,
        "assistant_tokens": assistant_tokens,
        "tool_tokens": tool_tokens,
        "tool_result_by_name": tool_result_by_name,
        "total_tokens": actual_input_tokens,
        "is_plan_mode": is_plan_mode,
        "plan_mode_tokens": plan_mode_tokens,
    }


@router.delete("/api/sessions/{session_id}")
def api_delete_session(session_id: str) -> dict[str, bool]:
    print(f"[DELETE] session_id={session_id}")
    success = delete_session(session_id)
    if not success:
        raise HTTPException(status_code=404, detail="Session not found")
    return {"success": True}


async def generate_session_title(message: str) -> str:
    """使用 title agent 生成会话标题。"""
    import asyncio
    import sys
    fallback_name = message[:20] + "..." if len(message) > 20 else message
    print(f"[TITLE] Starting title generation for message: {message[:50]}...", file=sys.stderr)
    try:
        from agents.agent import Agent
        from agents.core.agent_mode import BUILTIN_HIDDEN_AGENTS
        from agents.config import load_config
        from agents.service import AgentService
        
        title_config = BUILTIN_HIDDEN_AGENTS.get("side_query_title")
        if not title_config:
            return fallback_name
        
        config = load_config()
        candidates = []
        side_model_id = config.routing.get('side')
        if side_model_id and side_model_id in config.endpoints:
            candidates.append(config.endpoints[side_model_id])
        for eid, ep in config.endpoints.items():
            if eid != side_model_id:
                candidates.append(ep)
        
        if not candidates:
            return fallback_name
        
        for endpoint in candidates:
            async def _run_title_agent(endpoint=endpoint) -> str:
                from agents.observability.trace import trace_context

                with trace_context(
                    trace_name="session-title",
                    tags=["session-title", "side-query"],
                    metadata={"model": endpoint.model},
                ):
                    agent = Agent(
                        model=endpoint.model,
                        api_key=endpoint.api_key,
                        api_base=endpoint.base_url,
                        custom_system_prompt=title_config.system_prompt,
                        custom_tools=[],
                        is_sub_agent=True,
                    )
                    svc = AgentService(agent)
                    await svc.run_once(message)
                    return svc.last_response.strip()

            try:
                name = await asyncio.wait_for(asyncio.create_task(_run_title_agent()), timeout=30.0)
                print(f"[TITLE] Generated name: '{name}' (len={len(name)})")

                if name:
                    name = name.replace('"', '').replace("'", "").strip()
                    return name[:30] if len(name) > 30 else name
            except Exception as e:
                import traceback
                print(f"[TITLE] Endpoint {endpoint.model} failed: {e}")
                print(f"[TITLE] Traceback: {traceback.format_exc()}")
                continue
        
        print(f"[TITLE] All endpoints failed, returning fallback: {fallback_name}")
        return fallback_name
    except Exception as e:
        import traceback
        print(f"[TITLE] Outer exception: {e}")
        print(f"[TITLE] Traceback: {traceback.format_exc()}")
        return fallback_name


@router.post("/api/sessions/{session_id}/abort")
async def api_abort_session(session_id: str) -> dict[str, Any]:
    """取消正在进行的 turn
    
    参考 deepseek-harness 的设计：
    1. 设置 abort 标志
    2. 等待当前 step 完成（由 agent_loop 检查 abort 标志）
    3. 清理 inbox（待处理的消息）
    4. 记录 cancel 事件
    """
    from agents.session_manager import get_session_manager
    sm = get_session_manager()
    
    # 使用session_manager来abort
    agent = sm.get_agent(session_id)
    session = sm.get(session_id)
    
    if agent and session:
        try:
            agent.abort()
            
            # 记录 cancel 事件到 session
            session.append("turn/cancel", {
                "reason": "user_abort",
                "turn": agent._current_turn,
            })
            
            return {"success": True, "message": "Abort requested"}
        except Exception as e:
            return {"success": False, "message": f"Abort failed: {e}"}
    
    return {"success": False, "message": "Session not active"}


@router.post("/api/sessions/{session_id}/compact")
async def api_compact_session(session_id: str) -> dict[str, Any]:
    from agents.core.session import get_session_backend
    backend = get_session_backend()
    events = backend.load_all_events(session_id)
    if not events:
        raise HTTPException(status_code=404, detail="Session not found")
    
    session_info = _active_sessions.get(session_id)
    if session_info and session_info.get("svc"):
        svc = session_info["svc"]
        try:
            await svc.compact()
            
            # 获取压缩后的 token 计数
            stats = svc.get_stats()
            token_count = stats.get("last_input_tokens", 0)
            context_window = stats.get("context_window", 200000)
            
            # 发送压缩事件到前端
            from frontend.server.routers.websocket import broadcast_event
            broadcast_event(session_id, {
                "type": "context/compacted",
                "message": "上下文已压缩",
                "last_input_token_count": token_count,
                "context_window": context_window,
            })
            
            return {"success": True, "message": "Context compacted"}
        except Exception as e:
            return {"success": False, "message": str(e)}
    
    return {"success": False, "message": "Session not active"}


# ── 统一回退 API（对话 + 文件原子回退，三阶段） ──


@router.post("/api/sessions/{session_id}/rewind/stage")
async def api_rewind_stage(session_id: str, data: RewindStageRequest) -> dict[str, Any]:
    """Stage：生成回退计划（文件 diff + 对话截断预览），不修改任何状态。"""
    from agents.core.rewind_service import get_rewind_service
    try:
        session_info = _active_sessions.get(session_id)
        if session_info and session_info.get("svc"):
            plan = await session_info["svc"].rewind_stage(
                turns=data.turns, keep_user_messages=data.keep_user_messages
            )
        else:
            plan_obj = await get_rewind_service().stage(
                session_id, turns=data.turns, keep_user_messages=data.keep_user_messages
            )
            plan = plan_obj.to_dict()
        return {"success": True, "plan": plan}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/api/sessions/{session_id}/rewind/commit")
async def api_rewind_commit(session_id: str, data: RewindCommitRequest) -> dict[str, Any]:
    """Commit：执行回退（恢复文件 → 截断事件日志 → 追加 rewind 标记事件）。"""
    from agents.core.rewind_service import get_rewind_service
    try:
        session_info = _active_sessions.get(session_id)
        if session_info and session_info.get("svc"):
            result = await session_info["svc"].rewind_commit(data.plan_id)
        else:
            result = await get_rewind_service().commit(data.plan_id)
        return {"success": True, **result}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/api/sessions/{session_id}/rewind/clear")
async def api_rewind_clear(session_id: str, data: RewindClearRequest) -> dict[str, Any]:
    """Clear：取消回退计划（stage 未修改文件，直接丢弃）。"""
    from agents.core.rewind_service import get_rewind_service
    try:
        result = await get_rewind_service().clear(data.plan_id)
        return {"success": True, **result}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/api/sessions/{session_id}/rewind")
async def api_rewind_session(session_id: str, data: RewindRequest) -> dict[str, Any]:
    """原子回退 N 轮（对话 + 文件），用于 CLI / composer 等非交互场景。"""
    from agents.core.rewind_service import get_rewind_service
    try:
        session_info = _active_sessions.get(session_id)
        if session_info and session_info.get("svc"):
            message = await session_info["svc"].rewind(data.turns)
            return {"success": True, "message": message}
        svc = get_rewind_service()
        plan = await svc.stage(session_id, turns=data.turns)
        result = await svc.commit(plan.id)
        return {
            "success": True,
            "message": f"Rewound {data.turns} turn(s), restored {len(result['restored_files'])} files",
            **result,
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/api/sessions/{session_id}/permission-mode")
async def api_get_permission_mode(session_id: str) -> dict[str, Any]:
    session_info = _active_sessions.get(session_id)
    if session_info and session_info.get("svc"):
        svc = session_info["svc"]
        mode = getattr(svc, 'permission_mode', 'default')
        return {"permission_mode": mode}
    
    return {"permission_mode": "default"}


@router.put("/api/sessions/{session_id}/permission-mode")
async def api_update_permission_mode(session_id: str, data: PermissionModeRequest) -> dict[str, Any]:
    session_info = _active_sessions.get(session_id)
    if session_info and session_info.get("svc"):
        svc = session_info["svc"]
        svc.set_permission_mode(data.mode)
        return {"success": True, "permission_mode": data.mode}
    
    return {"success": False, "message": "Session not active"}


@router.get("/api/sessions/{session_id}/token-breakdown")
async def api_get_token_breakdown(session_id: str) -> dict[str, Any]:
    """获取实际发给模型的 token 分解
    
    从最新的 stats 事件读取细粒度 breakdown 数据
    如果 stats 事件没有 breakdown 数据，从当前 agent 实时计算
    """
    session_info = _active_sessions.get(session_id)
    
    if session_info and session_info.get("svc"):
        svc = session_info["svc"]
        agent = getattr(svc, '_agent', None)
        if agent:
            # 从 session 获取最新的 stats 事件
            session = getattr(svc, 'session', None) or getattr(agent, 'session', None)
            latest_stats = {}
            if session:
                for event in reversed(session._log):
                    if event.get("type") == "stats":
                        latest_stats = event
                        break
            
            # 检查是否有 breakdown 数据
            has_breakdown = latest_stats.get("system_chars", 0) > 0 or latest_stats.get("user_chars", 0) > 0
            
            if has_breakdown:
                # 使用 stats 事件中的 breakdown 数据
                actual_input_tokens = latest_stats.get("last_input_token_count", 0)
                system_chars = latest_stats.get("system_chars", 0)
                user_chars = latest_stats.get("user_chars", 0)
                assistant_chars = latest_stats.get("assistant_chars", 0)
                tool_result_chars = latest_stats.get("tool_result_chars", 0)
                
                system_base_chars = latest_stats.get("system_base_chars", 0)
                system_claude_md_chars = latest_stats.get("system_claude_md_chars", 0)
                system_skills_chars = latest_stats.get("system_skills_chars", 0)
                system_memory_chars = latest_stats.get("system_memory_chars", 0)
                system_wiki_chars = latest_stats.get("system_wiki_chars", 0)
                system_agents_chars = latest_stats.get("system_agents_chars", 0)
                system_workspace_chars = latest_stats.get("system_workspace_chars", 0)
                
                total_chars = system_chars + user_chars + assistant_chars + tool_result_chars
                
                # 获取 MCP 工具数量
                from agents.tools.registry import get_active_tool_definitions
                tools = getattr(agent, 'tools', [])
                tool_defs = get_active_tool_definitions(tools)
                mcp_tool_count = sum(1 for t in tool_defs if t.get('name', '').startswith('mcp__'))
                builtin_tool_count = len(tool_defs) - mcp_tool_count
                
                # 按比例分配实际 token 数
                if actual_input_tokens > 0 and total_chars > 0:
                    scale = actual_input_tokens / (total_chars / 4)
                    system_tokens = int((system_chars / 4) * scale)
                    user_tokens = int((user_chars / 4) * scale)
                    assistant_tokens = int((assistant_chars / 4) * scale)
                    tool_tokens = int((tool_result_chars / 4) * scale)
                    
                    base_prompt_tokens = int(((system_base_chars + system_workspace_chars) / 4) * scale)
                    claude_md_tokens = int((system_claude_md_chars / 4) * scale)
                    skills_tokens = int((system_skills_chars / 4) * scale)
                    memory_tokens = int((system_memory_chars / 4) * scale)
                    wiki_tokens = int((system_wiki_chars / 4) * scale)
                    agents_tokens = int((system_agents_chars / 4) * scale)
                else:
                    system_tokens = system_chars // 4
                    user_tokens = user_chars // 4
                    assistant_tokens = assistant_chars // 4
                    tool_tokens = tool_result_chars // 4
                    base_prompt_tokens = (system_base_chars + system_workspace_chars) // 4
                    claude_md_tokens = system_claude_md_chars // 4
                    skills_tokens = system_skills_chars // 4
                    memory_tokens = system_memory_chars // 4
                    wiki_tokens = system_wiki_chars // 4
                    agents_tokens = system_agents_chars // 4
                
                messages_tokens = user_tokens + assistant_tokens + tool_tokens
                
                is_plan_mode = latest_stats.get("is_plan_mode", False)
                plan_mode_chars = latest_stats.get("plan_mode_chars", 0)
                plan_mode_tokens = int((plan_mode_chars / 4) * scale) if actual_input_tokens > 0 and total_chars > 0 else plan_mode_chars // 4
                
                # 按工具名拆分的结果 token
                tool_result_by_name_chars = latest_stats.get("tool_result_by_name", {})
                tool_result_by_name = {}
                if tool_result_by_name_chars and tool_tokens > 0:
                    total_tool_chars = sum(tool_result_by_name_chars.values())
                    if total_tool_chars > 0:
                        for tool_name, chars in tool_result_by_name_chars.items():
                            tool_result_by_name[tool_name] = int(tool_tokens * (chars / total_tool_chars))
                
                return {
                    "base_prompt_tokens": base_prompt_tokens,
                    "claude_md_tokens": claude_md_tokens,
                    "skills_tokens": skills_tokens,
                    "memory_tokens": memory_tokens,
                    "wiki_tokens": wiki_tokens,
                    "agents_tokens": agents_tokens,
                    "tools_tokens": tool_tokens,
                    "builtin_tool_count": builtin_tool_count,
                    "mcp_tool_count": mcp_tool_count,
                    "messages_tokens": messages_tokens,
                    "message_count": latest_stats.get("msg_count", 0),
                    "user_tokens": user_tokens,
                    "assistant_tokens": assistant_tokens,
                    "tool_tokens": tool_tokens,
                    "tool_result_by_name": tool_result_by_name,
                    "total_tokens": actual_input_tokens,
                    "is_plan_mode": is_plan_mode,
                    "plan_mode_tokens": plan_mode_tokens,
                }
            
            # Fallback: 从当前 agent 实时计算
            messages = agent.messages or []
            system_chars = 0
            user_chars = 0
            assistant_chars = 0
            tool_result_chars = 0
            message_count = 0
            
            for msg in messages:
                role = msg.get('role', '')
                content = msg.get('content', '')
                if isinstance(content, str):
                    chars = len(content)
                elif isinstance(content, list):
                    chars = sum(len(item.get('text', '')) for item in content if isinstance(item, dict) and item.get('type') == 'text')
                else:
                    chars = 0
                
                if role == 'system':
                    system_chars = chars
                elif role == 'user':
                    user_chars += chars
                    message_count += 1
                elif role == 'assistant':
                    assistant_chars += chars
                    message_count += 1
                elif role == 'tool':
                    tool_result_chars += chars
                    message_count += 1
            
            # 获取 tools
            from agents.tools.registry import get_active_tool_definitions
            tools = getattr(agent, 'tools', [])
            tool_defs = get_active_tool_definitions(tools)
            tools_json = json.dumps([{
                'name': t.get('name', ''),
                'description': t.get('description', ''),
                'parameters': t.get('parameters', {}),
            } for t in tool_defs], ensure_ascii=False)
            tools_chars = len(tools_json)
            mcp_tool_count = sum(1 for t in tool_defs if t.get('name', '').startswith('mcp__'))
            builtin_tool_count = len(tool_defs) - mcp_tool_count
            
            # 获取 system prompt 各部分
            try:
                from agents.core.prompt import (
                    load_claude_md, build_skill_descriptions,
                    build_memory_prompt_section, build_wiki_prompt_section,
                    build_agent_descriptions, build_workspace_structure
                )
                system_claude_md_chars = len(load_claude_md())
                system_skills_chars = len(build_skill_descriptions())
                system_memory_chars = len(build_memory_prompt_section())
                system_wiki_chars = len(build_wiki_prompt_section())
                system_agents_chars = len(build_agent_descriptions())
                system_workspace_chars = len(build_workspace_structure())
                system_base_chars = system_chars - system_claude_md_chars - system_skills_chars - system_memory_chars - system_wiki_chars - system_agents_chars - system_workspace_chars
            except:
                system_base_chars = system_chars
                system_claude_md_chars = 0
                system_skills_chars = 0
                system_memory_chars = 0
                system_wiki_chars = 0
                system_agents_chars = 0
                system_workspace_chars = 0
            
            # Plan mode
            is_plan_mode = getattr(agent, 'permission_mode', '') == 'plan'
            plan_mode_chars = 0
            if is_plan_mode and hasattr(agent, '_plan_mode_manager') and agent._plan_mode_manager:
                try:
                    plan_mode_chars = len(agent._plan_mode_manager.build_plan_mode_prompt())
                except:
                    pass
            
            actual_input_tokens = getattr(agent, 'last_input_token_count', 0)
            total_chars = system_chars + tools_chars + user_chars + assistant_chars + tool_result_chars
            
            # 按比例分配
            if actual_input_tokens > 0 and total_chars > 0:
                scale = actual_input_tokens / (total_chars / 4)
                base_prompt_tokens = int(((system_base_chars + system_workspace_chars) / 4) * scale)
                claude_md_tokens = int((system_claude_md_chars / 4) * scale)
                skills_tokens = int((system_skills_chars / 4) * scale)
                memory_tokens = int((system_memory_chars / 4) * scale)
                wiki_tokens = int((system_wiki_chars / 4) * scale)
                agents_tokens = int((system_agents_chars / 4) * scale)
                tools_tokens = int((tools_chars / 4) * scale)
                user_tokens = int((user_chars / 4) * scale)
                assistant_tokens = int((assistant_chars / 4) * scale)
                tool_tokens = int((tool_result_chars / 4) * scale)
                plan_mode_tokens = int((plan_mode_chars / 4) * scale)
            else:
                base_prompt_tokens = (system_base_chars + system_workspace_chars) // 4
                claude_md_tokens = system_claude_md_chars // 4
                skills_tokens = system_skills_chars // 4
                memory_tokens = system_memory_chars // 4
                wiki_tokens = system_wiki_chars // 4
                agents_tokens = system_agents_chars // 4
                tools_tokens = tools_chars // 4
                user_tokens = user_chars // 4
                assistant_tokens = assistant_chars // 4
                tool_tokens = tool_result_chars // 4
                plan_mode_tokens = plan_mode_chars // 4
            
            messages_tokens = user_tokens + assistant_tokens + tool_tokens
            
            # 按工具名拆分的结果 token
            tool_result_by_name_chars = getattr(agent, '_tool_result_chars', {})
            tool_result_by_name = {}
            if tool_result_by_name_chars and tool_tokens > 0:
                total_tool_chars = sum(tool_result_by_name_chars.values())
                if total_tool_chars > 0:
                    for tool_name, chars in tool_result_by_name_chars.items():
                        tool_result_by_name[tool_name] = int(tool_tokens * (chars / total_tool_chars))
            
            return {
                "base_prompt_tokens": base_prompt_tokens,
                "claude_md_tokens": claude_md_tokens,
                "skills_tokens": skills_tokens,
                "memory_tokens": memory_tokens,
                "wiki_tokens": wiki_tokens,
                "agents_tokens": agents_tokens,
                "tools_tokens": tools_tokens,
                "builtin_tool_count": builtin_tool_count,
                "mcp_tool_count": mcp_tool_count,
                "messages_tokens": messages_tokens,
                "message_count": message_count,
                "user_tokens": user_tokens,
                "assistant_tokens": assistant_tokens,
                "tool_tokens": tool_tokens,
                "tool_result_by_name": tool_result_by_name,
                "total_tokens": actual_input_tokens if actual_input_tokens > 0 else total_chars // 4,
                "is_plan_mode": is_plan_mode,
                "plan_mode_tokens": plan_mode_tokens,
            }
    
    # Fallback: 从 JSONL 事件恢复
    sessions_dir = session_dir()
    events_file = sessions_dir / f"{session_id}.events.jsonl"
    
    if not events_file.exists():
        return {
            "base_prompt_tokens": 0, "claude_md_tokens": 0, "skills_tokens": 0,
            "memory_tokens": 0, "wiki_tokens": 0, "agents_tokens": 0,
            "tools_tokens": 0, "builtin_tool_count": 0, "mcp_tool_count": 0,
            "messages_tokens": 0, "message_count": 0,
            "user_tokens": 0, "assistant_tokens": 0, "tool_tokens": 0,
            "total_tokens": 0,
            "is_plan_mode": False, "plan_mode_tokens": 0,
        }
    
    # 从最新的 stats 事件读取
    latest_stats = {}
    with open(events_file) as f:
        for line in f:
            try:
                event = json.loads(line.strip())
                if event.get("type") == "stats":
                    latest_stats = event
            except:
                continue
    
    if not latest_stats:
        return {
            "base_prompt_tokens": 0, "claude_md_tokens": 0, "skills_tokens": 0,
            "memory_tokens": 0, "wiki_tokens": 0, "agents_tokens": 0,
            "tools_tokens": 0, "builtin_tool_count": 0, "mcp_tool_count": 0,
            "messages_tokens": 0, "message_count": 0,
            "user_tokens": 0, "assistant_tokens": 0, "tool_tokens": 0,
            "total_tokens": 0,
            "is_plan_mode": False, "plan_mode_tokens": 0,
        }
    
    actual_input_tokens = latest_stats.get("last_input_token_count", 0)
    system_chars = latest_stats.get("system_chars", 0)
    user_chars = latest_stats.get("user_chars", 0)
    assistant_chars = latest_stats.get("assistant_chars", 0)
    tool_result_chars = latest_stats.get("tool_result_chars", 0)
    
    # 检查是否有 breakdown 数据
    has_breakdown = system_chars > 0 or user_chars > 0 or assistant_chars > 0 or tool_result_chars > 0
    
    if not has_breakdown:
        # 从事件重建 messages
        system_chars = 0
        user_chars = 0
        assistant_chars = 0
        tool_result_chars = 0
        message_count = 0
        
        with open(events_file) as f:
            for line in f:
                try:
                    event = json.loads(line.strip())
                    event_type = event.get("type", "")
                    
                    if event_type == "system_prompt":
                        system_chars = len(event.get("content", ""))
                    elif event_type == "user_message":
                        user_chars += len(event.get("content", ""))
                        message_count += 1
                    elif event_type == "assistant_message":
                        assistant_chars += len(event.get("content", ""))
                        message_count += 1
                    elif event_type == "tool_result_msg":
                        tool_result_chars += len(event.get("content", ""))
                        message_count += 1
                except:
                    continue
        
        # 估算 system prompt 各部分（使用默认值）
        if system_chars == 0:
            system_chars = 6000  # 默认系统提示词大小
        system_base_chars = int(system_chars * 0.5)  # 基础提示词占 50%
        system_claude_md_chars = int(system_chars * 0.2)  # CLAUDE.md 占 20%
        system_skills_chars = int(system_chars * 0.1)  # Skills 占 10%
        system_memory_chars = int(system_chars * 0.05)  # Memory 占 5%
        system_wiki_chars = int(system_chars * 0.05)  # Wiki 占 5%
        system_agents_chars = int(system_chars * 0.05)  # Agents 占 5%
        system_workspace_chars = int(system_chars * 0.05)  # Workspace 占 5%
    else:
        system_base_chars = latest_stats.get("system_base_chars", 0)
        system_claude_md_chars = latest_stats.get("system_claude_md_chars", 0)
        system_skills_chars = latest_stats.get("system_skills_chars", 0)
        system_memory_chars = latest_stats.get("system_memory_chars", 0)
        system_wiki_chars = latest_stats.get("system_wiki_chars", 0)
        system_agents_chars = latest_stats.get("system_agents_chars", 0)
        system_workspace_chars = latest_stats.get("system_workspace_chars", 0)
        message_count = latest_stats.get("msg_count", 0)
    
    # 计算 tools 定义的字符数（用于缩放计算）
    try:
        from frontend.server.mcp_manager import global_mcp_manager
        tool_defs = global_mcp_manager.get_tool_definitions()
        tools_json = json.dumps([{
            'name': t.get('name', ''),
            'description': t.get('description', ''),
            'parameters': t.get('parameters', {}),
        } for t in tool_defs], ensure_ascii=False)
        tools_chars = len(tools_json)
        builtin_tool_count = 0
        mcp_tool_count = len(tool_defs)
    except:
        tools_chars = 0
        builtin_tool_count = latest_stats.get("tool_count", 0)
        mcp_tool_count = 0
    
    # total_chars 包含 tools
    total_chars = system_chars + tools_chars + user_chars + assistant_chars + tool_result_chars
    
    if actual_input_tokens > 0 and total_chars > 0:
        scale = actual_input_tokens / (total_chars / 4)
        system_tokens = int((system_chars / 4) * scale)
        user_tokens = int((user_chars / 4) * scale)
        assistant_tokens = int((assistant_chars / 4) * scale)
        tool_tokens = int((tool_result_chars / 4) * scale)
        tools_definition_tokens = int((tools_chars / 4) * scale)
        
        base_prompt_tokens = int(((system_base_chars + system_workspace_chars) / 4) * scale)
        claude_md_tokens = int((system_claude_md_chars / 4) * scale)
        skills_tokens = int((system_skills_chars / 4) * scale)
        memory_tokens = int((system_memory_chars / 4) * scale)
        wiki_tokens = int((system_wiki_chars / 4) * scale)
        agents_tokens = int((system_agents_chars / 4) * scale)
    else:
        system_tokens = system_chars // 4
        user_tokens = user_chars // 4
        assistant_tokens = assistant_chars // 4
        tool_tokens = tool_result_chars // 4
        tools_definition_tokens = tools_chars // 4
        base_prompt_tokens = (system_base_chars + system_workspace_chars) // 4
        claude_md_tokens = system_claude_md_chars // 4
        skills_tokens = system_skills_chars // 4
        memory_tokens = system_memory_chars // 4
        wiki_tokens = system_wiki_chars // 4
        agents_tokens = system_agents_chars // 4
    
    messages_tokens = user_tokens + assistant_tokens + tool_tokens
    
    # Plan mode
    is_plan_mode = latest_stats.get("is_plan_mode", False)
    plan_mode_chars = latest_stats.get("plan_mode_chars", 0)
    plan_mode_tokens = int((plan_mode_chars / 4) * scale) if actual_input_tokens > 0 and total_chars > 0 else plan_mode_chars // 4
    
    # 按工具名拆分的结果 token
    tool_result_by_name_chars = latest_stats.get("tool_result_by_name", {})
    tool_result_by_name = {}
    if tool_result_by_name_chars and tool_tokens > 0:
        total_tool_chars = sum(tool_result_by_name_chars.values())
        if total_tool_chars > 0:
            for tool_name, chars in tool_result_by_name_chars.items():
                tool_result_by_name[tool_name] = int(tool_tokens * (chars / total_tool_chars))
    
    return {
        "base_prompt_tokens": base_prompt_tokens,
        "claude_md_tokens": claude_md_tokens,
        "skills_tokens": skills_tokens,
        "memory_tokens": memory_tokens,
        "wiki_tokens": wiki_tokens,
        "agents_tokens": agents_tokens,
        "tools_tokens": tools_definition_tokens,
        "builtin_tool_count": builtin_tool_count,
        "mcp_tool_count": mcp_tool_count,
        "messages_tokens": messages_tokens,
        "message_count": message_count if 'message_count' in dir() else latest_stats.get("msg_count", 0),
        "user_tokens": user_tokens,
        "assistant_tokens": assistant_tokens,
        "tool_tokens": tool_tokens,
        "tool_result_by_name": tool_result_by_name,
        "total_tokens": actual_input_tokens,
        "is_plan_mode": is_plan_mode,
        "plan_mode_tokens": plan_mode_tokens,
    }


@router.post("/api/sessions/{session_id}/steer")
async def api_steer_session(session_id: str, data: SteerRequest) -> dict[str, Any]:
    session_info = _active_sessions.get(session_id)
    if session_info and session_info.get("svc"):
        svc = session_info["svc"]
        svc.steer(data.message)
        return {"success": True, "message": "Steer message queued"}
    
    return {"success": False, "message": "Session not active or steer not supported"}


@router.post("/api/sessions/{session_id}/fork")
async def api_fork_session(session_id: str, data: Optional[ForkRequest] = None) -> dict[str, Any]:
    """原子 Fork：从指定位置切割，创建新 session
    
    支持两种模式：
    1. at_seq: 从指定 seq 切割（推荐）
    2. keep_user_messages: 保留前 N 条用户消息（向后兼容）
    """
    print(f"[FORK] source session_id={session_id}, data={data}")
    
    from agents.session_manager import get_session_manager
    from agents.core.session import Session, get_session_backend, session_dir
    
    sm = get_session_manager()
    agent = sm.get_agent(session_id)
    session = sm.get(session_id)
    
    if agent and session:
        print(f"[FORK] Session {session_id} found in memory, saving...")
        await agent.save()
    
    backend = get_session_backend()
    events = backend.load_all_events(session_id)
    if not events:
        raise HTTPException(status_code=404, detail="Session not found")
    
    original_title = None
    loaded = None
    if session is not None:
        original_title = session.projections.get("title")
    if not original_title:
        loaded = Session.load_from_events(session_id)
        if loaded is not None:
            original_title = loaded.projections.get("title")
    print(f"[FORK] original: title={original_title}, events={len(events)}")
    
    new_session_id = uuid.uuid4().hex[:8]
    
    base_name = original_title or session_id
    if "(fork " in base_name:
        base_name = base_name.rsplit("(fork ", 1)[0]
    
    all_sessions = list_sessions()
    fork_num = 1
    while True:
        fork_name = f"{base_name}(fork {fork_num})"
        exists = any(s.get("name") == fork_name for s in all_sessions)
        if not exists:
            break
        fork_num += 1
    
    print(f"[FORK] new: id={new_session_id}, name={fork_name}")
    
    truncated_events = list(events)
    if data and data.at_seq is not None:
        truncated_events = [e for e in events if e.get("seq", 0) < data.at_seq]
        print(f"[FORK] truncating events at seq={data.at_seq}, kept={len(truncated_events)}")
    elif data and data.keep_user_messages is not None:
        user_count = 0
        keep_until = len(truncated_events)
        for i, event in enumerate(truncated_events):
            if event.get("type") == "user_message":
                user_count += 1
                if user_count >= data.keep_user_messages:
                    for j in range(i + 1, len(truncated_events)):
                        if truncated_events[j].get("type") == "user_message":
                            keep_until = j
                            break
                    else:
                        keep_until = len(truncated_events)
                    break
        truncated_events = truncated_events[:keep_until]
        print(f"[FORK] truncating events to keep {data.keep_user_messages} user messages, kept={len(truncated_events)}")
    
    copied_events = []
    for event in truncated_events:
        copied_event = dict(event)
        copied_event["session_id"] = new_session_id
        copied_events.append(copied_event)

    title_event = {
        "type": "session/title",
        "time": int(time.time() * 1000),
        "session_id": new_session_id,
        "title": fork_name,
        "seq": len(copied_events),
    }
    copied_events.append(title_event)
    
    from agents.core.session_backend_jsonl import JsonlSessionBackend
    new_backend = JsonlSessionBackend()
    for event in copied_events:
        new_backend.append(new_session_id, event)
    print(f"[FORK] wrote {len(copied_events)} events to {new_session_id}")
    
    try:
        from agents.core.session_projection_cache import (
            restore_projections,
            SessionHeader,
            FORMAT_VERSION,
        )
        cwd = session.projections.get("cwd") if session else None
        if not cwd and loaded is not None:
            cwd = loaded.projections.get("cwd")
        header = SessionHeader(
            id=new_session_id,
            version=FORMAT_VERSION,
            created_at=int(time.time() * 1000),
            cwd=cwd,
            is_seeded=False,
            inherited_event_count=len(copied_events),
        )
        projections = restore_projections(new_session_id, copied_events, header)
        print(f"[FORK] projcache built for {new_session_id}: title={projections.get('title')}")
    except Exception as e:
        print(f"[FORK] failed to build projcache: {e}")
    
    return {
        "success": True,
        "new_session_id": new_session_id,
        "fork_name": fork_name,
        "message": f"Session forked to {new_session_id}"
    }


def _session_workspace(session_id: str) -> Path:
    """解析会话的 workspace：活跃会话取 agent.workspace，否则读 projcache 的 cwd。"""
    info = _active_sessions.get(session_id)
    svc = info.get("svc") if info else None
    if svc is not None:
        return Path(svc.workspace)
    try:
        projcache = session_dir() / f"{session_id}.projcache.json"
        if projcache.exists():
            rows = json.loads(projcache.read_text()).get("rows", {})
            cwd = rows.get("cwd", {}).get("val")
            if cwd:
                return Path(cwd)
    except Exception:
        pass
    return Path.cwd()


@router.get("/api/sessions/{session_id}/plan/{slug}/status")
async def api_plan_status(session_id: str, slug: str) -> dict[str, Any]:
    """Get plan status and tasks."""
    with workspace_scope(_session_workspace(session_id)):
        try:
            from agents.plan.plan_manager import get_plan, get_tasks
            plan = get_plan(slug)
            if not plan:
                return {"success": False, "message": f"Plan '{slug}' not found"}
        
            tasks = get_tasks(slug)
            done = sum(1 for t in tasks if t.status == "done")
            failed = sum(1 for t in tasks if t.status == "failed")
            pending = sum(1 for t in tasks if t.status == "pending")
        
            return {
                "success": True,
                "data": {
                    "slug": plan.slug,
                    "status": plan.status.value,
                    "tasks": {
                        "total": len(tasks),
                        "done": done,
                        "failed": failed,
                        "pending": pending,
                    },
                    "task_list": [
                        {"id": t.id, "description": t.description, "status": t.status}
                        for t in tasks
                    ],
                }
            }
        except Exception as e:
            return {"success": False, "message": str(e)}


@router.post("/api/sessions/{session_id}/plan/{slug}/pause")
async def api_plan_pause(session_id: str, slug: str) -> dict[str, Any]:
    """Pause plan execution."""
    with workspace_scope(_session_workspace(session_id)):
        try:
            from agents.plan.plan_manager import pause_plan
            if pause_plan(slug):
                return {"success": True}
            return {"success": False, "message": "Cannot pause plan"}
        except Exception as e:
            return {"success": False, "message": str(e)}


@router.post("/api/sessions/{session_id}/plan/{slug}/resume")
async def api_plan_resume(session_id: str, slug: str) -> dict[str, Any]:
    """Resume plan execution."""
    with workspace_scope(_session_workspace(session_id)):
        try:
            from agents.plan.plan_manager import resume_plan
            if resume_plan(slug):
                return {"success": True}
            return {"success": False, "message": "Cannot resume plan"}
        except Exception as e:
            return {"success": False, "message": str(e)}


class SkipTaskRequest(BaseModel):
    task_id: int


@router.post("/api/sessions/{session_id}/plan/{slug}/skip-task")
async def api_plan_skip_task(session_id: str, slug: str, data: SkipTaskRequest) -> dict[str, Any]:
    """Skip a task in plan."""
    with workspace_scope(_session_workspace(session_id)):
        try:
            from agents.plan.plan_manager import skip_task
            if skip_task(slug, data.task_id):
                return {"success": True}
            return {"success": False, "message": "Cannot skip task"}
        except Exception as e:
            return {"success": False, "message": str(e)}


class RedoTaskRequest(BaseModel):
    task_id: int


@router.post("/api/sessions/{session_id}/plan/{slug}/redo-task")
async def api_plan_redo_task(session_id: str, slug: str, data: RedoTaskRequest) -> dict[str, Any]:
    """Redo a done/skipped task (reset to pending)."""
    with workspace_scope(_session_workspace(session_id)):
        try:
            from agents.plan.plan_manager import redo_task
            if redo_task(slug, data.task_id):
                return {"success": True}
            return {"success": False, "message": "Cannot redo task"}
        except Exception as e:
            return {"success": False, "message": str(e)}


@router.post("/api/sessions/{session_id}/plan/{slug}/abandon")
async def api_plan_abandon(session_id: str, slug: str) -> dict[str, Any]:
    """Abandon plan."""
    with workspace_scope(_session_workspace(session_id)):
        try:
            from agents.plan.plan_manager import abandon_plan
            if abandon_plan(slug):
                return {"success": True}
            return {"success": False, "message": "Cannot abandon plan"}
        except Exception as e:
            return {"success": False, "message": str(e)}


@router.get("/api/sessions/{session_id}/plan/{slug}/artifacts")
async def api_plan_artifacts(session_id: str, slug: str) -> dict[str, Any]:
    """Get plan artifacts (spec.md, tasks.md, design.md)."""
    with workspace_scope(_session_workspace(session_id)):
        try:
            from agents.plan.plan_manager import get_plan, read_artifact
            plan = get_plan(slug)
            if not plan:
                return {"success": False, "message": f"Plan '{slug}' not found"}
        
            artifacts = {}
            for filename in ["spec.md", "tasks.md", "design.md"]:
                content = read_artifact(slug, filename)
                if content:
                    artifacts[filename] = content
        
            return {"success": True, "data": artifacts}
        except Exception as e:
            return {"success": False, "message": str(e)}


@router.get("/api/sessions/{session_id}/plan/{slug}/ledger")
async def api_plan_ledger(session_id: str, slug: str) -> dict[str, Any]:
    """Get plan ledger (execution history)."""
    with workspace_scope(_session_workspace(session_id)):
        try:
            from agents.plan.plan_manager import read_ledger
            entries = read_ledger(slug)
            return {"success": True, "data": entries}
        except Exception as e:
            return {"success": False, "message": str(e)}


_DRAFT_ARTIFACT_NAMES = {"spec.md", "design.md", "tasks.md", "plan.md", "proposal.md"}


class DraftArtifactRequest(BaseModel):
    filename: str
    content: str


def _plan_draft_dir(session_id: str) -> Path:
    from agents.core.workspace import get_workspace
    return get_workspace() / ".mycode" / "plans" / f"plan-{session_id}"


@router.get("/api/sessions/{session_id}/plan-draft/artifacts")
async def api_plan_draft_artifacts(session_id: str) -> dict[str, Any]:
    """读取规划草稿目录（plan-{session_id}）中的 artifacts。"""
    with workspace_scope(_session_workspace(session_id)):
        draft_dir = _plan_draft_dir(session_id)
        if not draft_dir.exists():
            return {"success": False, "message": "No plan draft found"}
        artifacts = {}
        for filename in sorted(_DRAFT_ARTIFACT_NAMES):
            f = draft_dir / filename
            if f.exists():
                artifacts[filename] = f.read_text(encoding="utf-8")
        return {"success": True, "data": {"draft_dir": str(draft_dir), "artifacts": artifacts}}


@router.put("/api/sessions/{session_id}/plan-draft/artifacts")
async def api_plan_draft_artifact_update(session_id: str, data: DraftArtifactRequest) -> dict[str, Any]:
    """编辑规划草稿 artifact（仅允许白名单文件名，限定草稿目录内）。"""
    if data.filename not in _DRAFT_ARTIFACT_NAMES:
        return {"success": False, "message": f"Invalid artifact filename: {data.filename}"}
    with workspace_scope(_session_workspace(session_id)):
        draft_dir = _plan_draft_dir(session_id).resolve()
        target = (draft_dir / data.filename).resolve()
        if not target.is_relative_to(draft_dir):
            return {"success": False, "message": "Invalid artifact path"}
        draft_dir.mkdir(parents=True, exist_ok=True)
        target.write_text(data.content, encoding="utf-8")
        return {"success": True}


@router.get("/api/sessions/{session_id}/stats")
def api_session_stats(session_id: str) -> dict[str, Any]:
    session_info = _active_sessions.get(session_id)
    if session_info and session_info.get("svc"):
        svc = session_info["svc"]
        stats = svc.get_stats()
        agent = svc.agent
        cached_tokens = getattr(agent, 'total_cached_tokens', 0)
        return {
            "input_tokens": stats.get("input", 0),
            "output_tokens": stats.get("output", 0),
            "cached_tokens": cached_tokens,
            "context_window": agent.context_window,
            "effective_window": agent.effective_window,
            "last_input_token_count": agent.last_input_token_count,
        }
    # Check if session exists by checking events file
    sessions_dir = session_dir()
    events_file = sessions_dir / f"{session_id}.events.jsonl"
    if events_file.exists():
        cached_tokens = 0
        total_cached_tokens = 0
        input_tokens = 0
        output_tokens = 0
        last_input_token_count = 0
        context_window = 128000
        with open(events_file) as f:
            for line in f:
                try:
                    event = json.loads(line.strip())
                    if event.get("type") == "stats":
                        cached_tokens = event.get("cached_tokens", 0)
                        total_cached_tokens = event.get("total_cached_tokens", total_cached_tokens + cached_tokens)
                        input_tokens = event.get("input_tokens", 0)
                        output_tokens = event.get("output_tokens", 0)
                        last_input_token_count = event.get("last_input_token_count", 0)
                        context_window = event.get("context_window", 128000)
                except (json.JSONDecodeError, Exception):
                    continue
        return {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cached_tokens": total_cached_tokens,
            "context_window": context_window,
            "effective_window": 108000,
            "last_input_token_count": last_input_token_count,
        }
    raise HTTPException(status_code=404, detail="Session not found")


@router.get("/api/sessions/{session_id}/compression-stats")
def api_compression_stats(session_id: str) -> dict[str, Any]:
    from agents.session_manager import get_session_manager
    sm = get_session_manager()
    session_info = _active_sessions.get(session_id)
    if session_info and session_info.get("svc"):
        svc = session_info["svc"]
        agent = svc.agent
        compressor = agent._compressor
        token_count = agent.last_input_token_count
        effective_window = compressor.effective_window
        utilization = token_count / effective_window if effective_window else 0
        stats = compressor.get_stats()
        folded_memories = []
        try:
            folded_memories = getattr(agent, "_folded_session_memories", [])
        except Exception:
            pass
        return {
            "utilization": round(utilization, 3),
            "token_count": token_count,
            "effective_window": effective_window,
            "context_window": agent.context_window,
            **stats,
            "folded_memories": folded_memories,
        }
    # Fallback: compute from JSONL events for inactive sessions
    sessions_dir = session_dir()
    events_file = sessions_dir / f"{session_id}.events.jsonl"

    if not events_file.exists():
        raise HTTPException(status_code=404, detail="Session not found")

    # Parse events to find last stats event and compression events
    token_count = 0
    context_window = 128000
    effective_window = 108800
    compression_events = []
    folded_memories = []

    with open(events_file) as f:
        for line in f:
            try:
                event = json.loads(line.strip())
                event_type = event.get("type", "")

                if event_type == "stats":
                    token_count = event.get("last_input_token_count", 0)
                    context_window = event.get("context_window", 128000)
                    effective_window = event.get("effective_window", 108800)
                elif event_type == "compression":
                    compression_events.append(event)
                elif event_type == "fold":
                    folded_memories.append({
                        "time": event.get("time", ""),
                        "trigger": event.get("trigger", "auto"),
                        "episode": event.get("episode", ""),
                        "working": event.get("working", ""),
                    })
            except (json.JSONDecodeError, Exception):
                continue

    utilization = token_count / effective_window if effective_window else 0

    # Compute compression stats from events
    l1_triggered = sum(1 for e in compression_events if e.get("level") == "l1_budget")
    l2_triggered = sum(1 for e in compression_events if e.get("level") == "l2_snip")
    l3_triggered = sum(1 for e in compression_events if e.get("level") == "l3_microcompact")
    l4_triggered = sum(1 for e in compression_events if e.get("level") == "l4_fold")

    return {
        "utilization": round(utilization, 3),
        "token_count": token_count,
        "effective_window": effective_window,
        "context_window": context_window,
        "l1_budget": {"triggered": l1_triggered, "tokens_saved": 0},
        "l2_snip": {"triggered": l2_triggered, "tokens_saved": 0},
        "l3_microcompact": {"triggered": l3_triggered, "tokens_saved": 0},
        "l4_fold": {"triggered": l4_triggered, "last_fold_time": None},
        "folded_memories": folded_memories,
    }


@router.get("/api/sessions/{session_id}/context-store")
def api_context_store(session_id: str) -> dict[str, Any]:
    """返回被隐藏的事件信息（in_message = False）。"""
    from agents.session_manager import get_session_manager
    sm = get_session_manager()
    session_info = _active_sessions.get(session_id)
    if session_info and session_info.get("svc"):
        svc = session_info["svc"]
        session = svc.agent.session
        entries = []
        for event in session._log:
            if not event.get("in_message", True):
                entries.append({
                    "seq": event.get("seq"),
                    "type": event.get("type"),
                    "call_id": event.get("call_id", ""),
                    "content_size": len(event.get("content", "")),
                })
        return {
            "entries": entries,
            "total_entries": len(entries),
            "total_raw_size": sum(e["content_size"] for e in entries),
            "active_entries": len([e for e in entries if e.get("type") == "tool_result_msg"]),
        }
    # Check if session exists by checking events file
    sessions_dir = session_dir()
    events_file = sessions_dir / f"{session_id}.events.jsonl"
    if events_file.exists():
        return {
            "entries": [],
            "total_entries": 0,
            "total_raw_size": 0,
            "active_entries": 0,
        }
    raise HTTPException(status_code=404, detail="Session not found")


@router.get("/api/sessions/{session_id}/file-changes")
async def api_session_file_changes(session_id: str) -> dict[str, Any]:
    """Get file changes for a session using git-based snapshots.
    
    Returns a list of file diffs between the first and last snapshot of the session.
    Each file includes path, status (added/modified/deleted), old_content, new_content.
    """
    from agents.core.snapshot_service import SnapshotService
    from agents.core.git_repository import GitRepositoryManager
    
    # Get session cwd from metadata or active session
    cwd = None
    session_info = _active_sessions.get(session_id)
    if session_info and session_info.get("svc"):
        svc = session_info["svc"]
        agent = svc.agent
        cwd = getattr(agent, 'workspace', None) or getattr(agent, 'cwd', None)
    
    if not cwd:
        # Try to get cwd from session metadata
        from agents.core.session import list_sessions
        for s in list_sessions():
            if s.get("id") == session_id:
                cwd = s.get("cwd")
                break
    
    if not cwd:
        return {"files": [], "total": 0}
    
    snapshot_dir = Path.home() / ".mycode" / "snapshots"
    snapshot_service = SnapshotService(cwd, snapshot_dir)
    git_repo = GitRepositoryManager(cwd, snapshot_dir)
    
    try:
        snapshots = await snapshot_service.list(session_id)
        if not snapshots:
            return {"files": [], "total": 0}
        
        # Get the first and last snapshot
        first_snapshot = snapshots[-1]  # oldest
        last_snapshot = snapshots[0]    # newest
        
        files = []
        
        if len(snapshots) == 1:
            # Only one snapshot, diff against current state
            inspection = await snapshot_service.inspect(first_snapshot.id)
            first_manifest = await snapshot_service.get_manifest(first_snapshot.id)
            
            for f in inspection.files:
                if f.path.startswith(".mycode/") or f.path.startswith("manifests/"):
                    continue
                    
                old_content = ""
                new_content = ""
                is_new = f.status == "added"
                
                # Get old content from first snapshot tree
                if f.status in ("modified", "deleted"):
                    old_content = await git_repo.get_file_content(first_manifest.tree_hash, f.path)
                
                # Get new content from current working tree
                if f.status in ("added", "modified"):
                    file_path = Path(cwd) / f.path
                    if file_path.exists():
                        new_content = file_path.read_text(encoding="utf-8", errors="replace")
                
                files.append({
                    "file_path": f.path,
                    "is_new": is_new,
                    "old_content": old_content,
                    "new_content": new_content,
                })
        else:
            # Diff between first and last snapshot
            diffs = await snapshot_service.diff(first_snapshot.id, last_snapshot.id)
            first_manifest = await snapshot_service.get_manifest(first_snapshot.id)
            last_manifest = await snapshot_service.get_manifest(last_snapshot.id)
            
            for f in diffs:
                if f.path.startswith(".mycode/") or f.path.startswith("manifests/"):
                    continue
                    
                old_content = ""
                new_content = ""
                is_new = f.status == "added"
                
                # Get old content from first snapshot tree
                if f.status in ("modified", "deleted"):
                    old_content = await git_repo.get_file_content(first_manifest.tree_hash, f.path)
                
                # Get new content from last snapshot tree
                if f.status in ("added", "modified"):
                    new_content = await git_repo.get_file_content(last_manifest.tree_hash, f.path)
                
                files.append({
                    "file_path": f.path,
                    "is_new": is_new,
                    "old_content": old_content,
                    "new_content": new_content,
                })
        
        return {"files": files, "total": len(files)}
    except Exception as e:
        return {"files": [], "total": 0, "error": str(e)}


@router.get("/api/sessions/{session_id}/events")
def api_get_session_events(
    session_id: str,
    before: Optional[int] = None,
    limit: int = 50,
    from_seq: Optional[int] = None,
    to_seq: Optional[int] = None,
) -> dict[str, Any]:
    """分页获取 session 事件（用于懒加载和 gap repair）。
    
    Args:
        session_id: Session ID
        before: 加载 seq < before 的事件（向前加载历史）
        limit: 每次加载的事件数量，默认 50
        from_seq: 加载 seq >= from_seq 的事件（gap repair 用）
        to_seq: 加载 seq < to_seq 的事件（gap repair 用）
    
    Returns:
        {
            "events": [...],
            "has_more": bool,
            "base_seq": int,
            "total_count": int,
        }
    """
    from agents.core.session import Session
    
    session = Session.load_from_events(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    
    all_events = list(session.events)
    total_count = len(all_events)
    
    # Gap repair: 范围查询
    if from_seq is not None or to_seq is not None:
        start = from_seq or 0
        end = to_seq or total_count
        events = [e for e in all_events if start <= e.get("seq", 0) < end]
        return {
            "events": events,
            "has_more": False,
            "base_seq": events[0]["seq"] if events else 0,
            "total_count": total_count,
        }
    
    # 懒加载: 向前加载历史
    if before is not None:
        events = [e for e in all_events if e.get("seq", 0) < before]
        events = events[-limit:]  # 取最近的 limit 条
        has_more = len(events) == limit and events[0]["seq"] > 0 if events else False
        return {
            "events": events,
            "has_more": has_more,
            "base_seq": events[0]["seq"] if events else 0,
            "total_count": total_count,
        }
    
    # 默认: 加载最近的 limit 条
    events = all_events[-limit:]
    has_more = len(events) == limit and len(all_events) > limit
    return {
        "events": events,
        "has_more": has_more,
        "base_seq": events[0]["seq"] if events else 0,
        "total_count": total_count,
    }
