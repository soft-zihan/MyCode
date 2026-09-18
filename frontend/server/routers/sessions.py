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

from agents.core.session import list_sessions, list_child_sessions, delete_session

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


class RevertStageRequest(BaseModel):
    target_seq: int


class RevertClearRequest(BaseModel):
    current_snapshot: list[dict]


class RevertCommitRequest(BaseModel):
    target_seq: int


class TruncateRequest(BaseModel):
    keep_user_messages: int


class ForkRequest(BaseModel):
    """原子 Fork 请求：从指定 seq 切割，创建新 session"""
    at_seq: Optional[int] = None  # 切割点，None 表示完整复制
    keep_user_messages: Optional[int] = None  # 保留的用户消息数（向后兼容）


class PermissionModeRequest(BaseModel):
    mode: str


class PermissionResponseRequest(BaseModel):
    request_id: str
    allowed: bool


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


@router.get("/api/sessions/{session_id}/children")
def api_list_child_sessions(session_id: str) -> list[dict[str, Any]]:
    """列出指定 session 的所有子 session（子智能体）。"""
    print(f"[LIST CHILDREN] parent_session_id={session_id}")
    children = list_child_sessions(session_id)
    children.sort(key=lambda s: s.get("startTime", ""), reverse=True)
    print(f"[LIST CHILDREN] found {len(children)} children")
    return children


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
        
        title_config = BUILTIN_HIDDEN_AGENTS.get("title")
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
            try:
                from agents.observability.trace import get_trace_session, set_trace_session
                saved_session_id = get_trace_session()
                
                # 使用特殊的 session_id，避免创建用户可见的 session
                agent = Agent(
                    model=endpoint.model,
                    api_key=endpoint.api_key,
                    api_base=endpoint.base_url,
                    custom_system_prompt=title_config.system_prompt,
                    custom_tools=[],  # 禁用工具，避免模型调用工具
                    is_sub_agent=True,  # 避免保存 session 文件
                )
                svc = AgentService(agent)
                await asyncio.wait_for(svc.run_once(message), timeout=30.0)
                name = svc.last_response.strip()
                print(f"[TITLE] Generated name: '{name}' (len={len(name)})")
                
                if saved_session_id:
                    set_trace_session(saved_session_id)
                
                if name:
                    name = name.replace('"', '').replace("'", "").strip()
                    return name[:30] if len(name) > 30 else name
            except (asyncio.TimeoutError, Exception) as e:
                import traceback
                print(f"[TITLE] Endpoint {endpoint.model} failed: {e}")
                print(f"[TITLE] Traceback: {traceback.format_exc()}")
                if saved_session_id:
                    set_trace_session(saved_session_id)
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


@router.post("/api/sessions/{session_id}/truncate")
async def api_truncate_session(session_id: str, data: TruncateRequest) -> dict[str, Any]:
    """原子 Truncate：保留前 N 条用户消息
    
    直接使用 JsonlSessionBackend 操作事件日志。
    """
    print(f"[TRUNCATE] session_id={session_id}, keep_user_messages={data.keep_user_messages}")
    
    from agents.core.session_backend_jsonl import JsonlSessionBackend
    backend = JsonlSessionBackend()
    
    events = backend.load_all_events(session_id)
    if not events:
        raise HTTPException(status_code=404, detail="Session not found")
    
    print(f"[TRUNCATE] before: {len(events)} events")
    
    user_count = 0
    truncate_at_seq = None
    
    for event in events:
        if event.get("type") == "user_message":
            if user_count >= data.keep_user_messages:
                truncate_at_seq = event.get("seq")
                break
            user_count += 1
    
    if truncate_at_seq is None:
        print(f"[TRUNCATE] no truncation needed, keep all {user_count} user messages")
        return {"success": True, "message": f"Truncated to {data.keep_user_messages} user messages"}
    
    print(f"[TRUNCATE] truncate at seq={truncate_at_seq}")
    backend.truncate(session_id, truncate_at_seq)
    print(f"[TRUNCATE] truncated events.jsonl")
    
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
    print(f"[REWIND] session_id={session_id}, turns={data.turns}")
    
    from agents.core.session_backend_jsonl import JsonlSessionBackend
    backend = JsonlSessionBackend()
    
    events = backend.load_all_events(session_id)
    if not events:
        raise HTTPException(status_code=404, detail="Session not found")
    
    turn_boundaries = [e for e in events if e.get("type") == "turn_boundary"]
    print(f"[REWIND] turnBoundaries: {len(turn_boundaries)}")
    
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
    
    truncate_at_seq = target_boundary.get("seq")
    print(f"[REWIND] target_turn={target_turn}, truncate_at_seq={truncate_at_seq}")
    backend.truncate(session_id, truncate_at_seq)
    
    session_info = _active_sessions.get(session_id)
    if session_info and session_info.get("svc"):
        svc = session_info["svc"]
        messages = svc.get_messages()
        user_count = 0
        for i, msg in enumerate(messages):
            if msg.get("role") == "user":
                user_count += 1
        target_user_count = target_boundary.get("user_message_count", 0)
        if target_user_count < user_count:
            truncate_at = len(messages)
            user_count = 0
            for i, msg in enumerate(messages):
                if msg.get("role") == "user":
                    if user_count >= target_user_count:
                        truncate_at = i
                        break
                    user_count += 1
            svc.truncate_messages_to(truncate_at)
    
    print(f"[REWIND] truncated to turn {target_turn}")
    return {
        "success": True, 
        "message": f"Rewound to turn {target_turn}",
        "turn": target_turn
    }


# ── 三阶段恢复 API ──


@router.post("/api/sessions/{session_id}/revert/stage")
async def api_revert_stage(session_id: str, data: RevertStageRequest) -> dict[str, Any]:
    """Stage：计算恢复计划，预览变更。"""
    print(f"[REVERT/STAGE] session_id={session_id}, target_seq={data.target_seq}")
    
    session_info = _active_sessions.get(session_id)
    if session_info and session_info.get("svc"):
        svc = session_info["svc"]
        try:
            plan = svc.stage_revert(data.target_seq)
            return {"success": True, "plan": plan}
        except Exception as e:
            return {"success": False, "message": str(e)}
    
    return {"success": False, "message": "Session not active"}


@router.post("/api/sessions/{session_id}/revert/clear")
async def api_revert_clear(session_id: str, data: RevertClearRequest) -> dict[str, Any]:
    """Clear：取消恢复，恢复到原始状态。"""
    print(f"[REVERT/CLEAR] session_id={session_id}")
    
    session_info = _active_sessions.get(session_id)
    if session_info and session_info.get("svc"):
        svc = session_info["svc"]
        try:
            result = svc.clear_revert(data.current_snapshot)
            return {"success": True, "result": result}
        except Exception as e:
            return {"success": False, "message": str(e)}
    
    return {"success": False, "message": "Session not active"}


@router.post("/api/sessions/{session_id}/revert/commit")
async def api_revert_commit(session_id: str, data: RevertCommitRequest) -> dict[str, Any]:
    """Commit：确认恢复。"""
    print(f"[REVERT/COMMIT] session_id={session_id}, target_seq={data.target_seq}")
    
    session_info = _active_sessions.get(session_id)
    if session_info and session_info.get("svc"):
        svc = session_info["svc"]
        try:
            result = svc.commit_revert(data.target_seq)
            return {"success": True, "result": result}
        except Exception as e:
            return {"success": False, "message": str(e)}
    
    return {"success": False, "message": "Session not active"}


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
    """获取 token 三段分解：system/tools/messages
    
    返回字符数（前端可用于计算比例），以及估算的 token 数（约 4 字符 = 1 token）
    支持活跃和非活跃 session（从 JSONL 事件恢复）
    """
    session_info = _active_sessions.get(session_id)
    
    # Try active session first
    if session_info and session_info.get("svc"):
        svc = session_info["svc"]
        agent = getattr(svc, '_agent', None)
        if agent:
            # System prompt
            system_prompt = getattr(agent, '_system_prompt', '') or ''
            system_chars = len(system_prompt)
            
            # Tools schema (JSON representation)
            tools = getattr(agent, 'tools', [])
            tools_schema = []
            for tool in tools:
                if not tool.get('deferred'):
                    tools_schema.append({
                        'name': tool.get('name', ''),
                        'description': tool.get('description', ''),
                        'parameters': tool.get('parameters', {}),
                    })
            tools_chars = len(json.dumps(tools_schema, ensure_ascii=False))
            
            # Messages
            session = getattr(svc, 'session', None) or getattr(agent, 'session', None)
            messages_chars = 0
            if session:
                messages = session.get_messages_for_llm()
                for msg in messages:
                    if msg.get('role') != 'system':
                        content = msg.get('content', '')
                        if isinstance(content, str):
                            messages_chars += len(content)
                        elif isinstance(content, list):
                            for item in content:
                                if isinstance(item, dict) and item.get('type') == 'text':
                                    messages_chars += len(item.get('text', ''))
            
            return {
                "system_chars": system_chars,
                "tools_chars": tools_chars,
                "messages_chars": messages_chars,
                "system_tokens": system_chars // 4,
                "tools_tokens": tools_chars // 4,
                "messages_tokens": messages_chars // 4,
            }
    
    # Fallback: compute from JSONL events for inactive sessions
    from pathlib import Path
    sessions_dir = Path.home() / ".mycode" / "sessions"
    events_file = sessions_dir / f"{session_id}.events.jsonl"
    
    if not events_file.exists():
        return {
            "system_chars": 0,
            "tools_chars": 0,
            "messages_chars": 0,
            "system_tokens": 0,
            "tools_tokens": 0,
            "messages_tokens": 0,
        }
    
    # Parse events to reconstruct token breakdown
    system_chars = 0
    tools_chars = 0
    messages_chars = 0
    
    with open(events_file) as f:
        for line in f:
            try:
                event = json.loads(line.strip())
                event_type = event.get("type", "")
                
                # Assistant message with tool calls -> tools
                if event_type == "assistant_message":
                    content = event.get("content", "")
                    if content:
                        messages_chars += len(content)
                    # Tool calls embedded in assistant_message
                    tool_calls = event.get("tool_calls", [])
                    if tool_calls:
                        for tc in tool_calls:
                            func = tc.get("function", {})
                            tools_chars += len(json.dumps(func.get("arguments", "{}"), ensure_ascii=False))
                
                # Tool results -> tools
                elif event_type == "tool_result_msg":
                    tools_chars += len(event.get("content", ""))
                
                # User messages -> messages
                elif event_type == "user_message":
                    messages_chars += len(event.get("content", ""))
                
            except (json.JSONDecodeError, Exception):
                continue
    
    # Estimate: if no system_prompt event, assume a default system prompt size
    if system_chars == 0:
        system_chars = 2000  # reasonable default
    
    return {
        "system_chars": system_chars,
        "tools_chars": tools_chars,
        "messages_chars": messages_chars,
        "system_tokens": system_chars // 4,
        "tools_tokens": tools_chars // 4,
        "messages_tokens": messages_chars // 4,
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
    
    title_event = {
        "type": "session/title",
        "time": int(time.time() * 1000),
        "session_id": new_session_id,
        "title": fork_name,
        "seq": len(truncated_events),
    }
    truncated_events.append(title_event)
    
    from agents.core.session_backend_jsonl import JsonlSessionBackend
    new_backend = JsonlSessionBackend()
    for event in truncated_events:
        new_backend.append(new_session_id, event)
    print(f"[FORK] wrote {len(truncated_events)} events to {new_session_id}")
    
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
            inherited_event_count=len(truncated_events),
        )
        projections = restore_projections(new_session_id, truncated_events, header)
        print(f"[FORK] projcache built for {new_session_id}: title={projections.get('title')}")
    except Exception as e:
        print(f"[FORK] failed to build projcache: {e}")
    
    # 复制 trace 文件
    try:
        from agents.observability.trace import trace_dir
        import shutil
        source_trace = trace_dir() / f"{session_id}.jsonl"
        if source_trace.exists():
            target_trace = trace_dir() / f"{new_session_id}.jsonl"
            shutil.copy2(source_trace, target_trace)
            print(f"[FORK] copied trace: {source_trace} -> {target_trace}")
        else:
            for f in trace_dir().glob(f"*_{session_id}.jsonl"):
                target_trace = trace_dir() / f.name.replace(session_id, new_session_id)
                shutil.copy2(f, target_trace)
                print(f"[FORK] copied trace: {f} -> {target_trace}")
                break
    except Exception as e:
        print(f"[FORK] failed to copy trace: {e}")
    
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


class UpdatePlanRequest(BaseModel):
    plan_file_path: str
    content: str


@router.post("/api/sessions/{session_id}/plan/update")
async def api_plan_update(session_id: str, data: UpdatePlanRequest) -> dict[str, Any]:
    """Update the plan file content."""
    try:
        plan_path = Path(data.plan_file_path)
        if not plan_path.exists():
            return {"success": False, "message": "Plan file not found"}
        plan_path.write_text(data.content, encoding="utf-8")
        return {"success": True}
    except Exception as e:
        return {"success": False, "message": str(e)}


@router.get("/api/sessions/{session_id}/stats")
def api_session_stats(session_id: str) -> dict[str, Any]:
    session_info = _active_sessions.get(session_id)
    if session_info and session_info.get("svc"):
        svc = session_info["svc"]
        stats = svc.get_stats()
        agent = svc.agent
        # Get cached tokens from agent if available
        cached_tokens = getattr(agent, 'cached_tokens', 0)
        return {
            "input_tokens": stats.get("input", 0),
            "output_tokens": stats.get("output", 0),
            "cached_tokens": cached_tokens,
            "context_window": agent.context_window,
            "effective_window": agent.effective_window,
            "last_input_token_count": agent.last_input_token_count,
        }
    # Check if session exists by checking events file
    from pathlib import Path
    sessions_dir = Path.home() / ".mycode" / "sessions"
    events_file = sessions_dir / f"{session_id}.events.jsonl"
    if events_file.exists():
        # Try to get cached_tokens from last stats event
        cached_tokens = 0
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
                        input_tokens = event.get("input_tokens", 0)
                        output_tokens = event.get("output_tokens", 0)
                        last_input_token_count = event.get("last_input_token_count", 0)
                        context_window = event.get("context_window", 128000)
                except (json.JSONDecodeError, Exception):
                    continue
        return {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cached_tokens": cached_tokens,
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
    from pathlib import Path
    sessions_dir = Path.home() / ".mycode" / "sessions"
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
    from pathlib import Path
    sessions_dir = Path.home() / ".mycode" / "sessions"
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
