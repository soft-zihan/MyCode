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

from agents.core.session import list_sessions, list_child_sessions, load_session, save_session, delete_session

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


class SessionNameRequest(BaseModel):
    message: str


class SessionUpdateRequest(BaseModel):
    name: Optional[str] = None


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
    data = load_session(session_id)
    if data is None:
        raise HTTPException(status_code=404, detail="Session not found")
    metadata = data.get("metadata", {})
    events = data.get("events", [])
    
    # Load projections from Session object
    projections = {}
    try:
        from agents.core.session import Session
        session = Session.load_from_jsonl(session_id)
        if session:
            projections = session.projections
    except Exception:
        pass
    
    print(f"[GET] result: name={metadata.get('name')}, events={len(events)}, projections={projections}")
    return {
        **data,
        "projections": projections,
    }


@router.put("/api/sessions/{session_id}")
def api_update_session(session_id: str, request: dict[str, Any]) -> dict[str, Any]:
    """更新 session 的 metadata（如 name）。"""
    print(f"[PUT] session_id={session_id}, request={request}")
    
    # 尝试更新磁盘上的 session
    from agents.core.session import save_session_meta
    success = save_session_meta(session_id, request)
    
    if not success:
        # 如果磁盘上没有，尝试更新内存中的 session
        try:
            from agents.session_manager import get_session_manager
            manager = get_session_manager()
            session = manager.get(session_id)
            if session:
                # 更新 session 的 title
                if "name" in request:
                    session.title = request["name"]
                print(f"[PUT] Updated in-memory session {session_id}")
                return {"success": True, "message": "Session updated in memory"}
        except Exception as e:
            print(f"[PUT] Error updating in-memory session: {e}")
        
        raise HTTPException(status_code=404, detail="Session not found")
    
    print(f"[PUT] Updated session {session_id}")
    return {"success": True, "message": "Session updated"}


@router.get("/api/sessions/{session_id}/projections")
def api_get_session_projections(session_id: str) -> dict[str, Any]:
    """快速获取 session 投影值（title, updatedAt, cwd, running）。"""
    try:
        from agents.core.session import Session
        session = Session.load_from_jsonl(session_id)
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


@router.post("/api/sessions/generate-name")
async def api_generate_session_name(data: SessionNameRequest) -> dict[str, str]:
    name = await generate_session_title(data.message)
    return {"name": name}


@router.put("/api/sessions/{session_id}")
def api_update_session(session_id: str, data: SessionUpdateRequest) -> dict[str, Any]:
    print(f"[UPDATE] session_id={session_id}, name={data.name}")
    session_data = load_session(session_id)
    if session_data is None:
        raise HTTPException(status_code=404, detail="Session not found")
    
    if data.name is not None:
        session_data["metadata"]["name"] = data.name
    
    save_session(session_id, session_data)
    return {"success": True}


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
    session_data = load_session(session_id)
    if session_data is None:
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
    
    同时更新 events（事件日志）和 openaiMessages（向后兼容）。
    """
    print(f"[TRUNCATE] session_id={session_id}, keep_user_messages={data.keep_user_messages}")
    
    session_data = load_session(session_id)
    if session_data is None:
        raise HTTPException(status_code=404, detail="Session not found")
    
    # 1. 截断 events（事件日志）
    events = session_data.get("events", [])
    print(f"[TRUNCATE] before: {len(events)} events")
    
    user_count = 0
    truncate_at = len(events)
    
    for i, event in enumerate(events):
        if event.get("type") == "user_message":
            if user_count >= data.keep_user_messages:
                truncate_at = i
                break
            user_count += 1
    
    print(f"[TRUNCATE] truncate events at={truncate_at}")
    session_data["events"] = events[:truncate_at]
    
    # 2. 截断 openaiMessages（向后兼容）
    openai_messages = session_data.get("openaiMessages", [])
    print(f"[TRUNCATE] before: {len(openai_messages)} openaiMessages")
    for i, msg in enumerate(openai_messages):
        if msg.get("role") == "user":
            content = msg.get("content", "")[:30]
            print(f"[TRUNCATE]   [{i}] user: {content}...")
    
    user_count = 0
    truncate_at = len(openai_messages)
    
    for i, msg in enumerate(openai_messages):
        if msg.get("role") == "user":
            if user_count >= data.keep_user_messages:
                truncate_at = i
                break
            user_count += 1
    
    print(f"[TRUNCATE] truncate openaiMessages at={truncate_at}")
    session_data["openaiMessages"] = openai_messages[:truncate_at]
    
    # 3. 清理 turnBoundaries
    turn_boundaries = session_data.get("turnBoundaries", [])
    session_data["turnBoundaries"] = [
        b for b in turn_boundaries 
        if b.get("message_count", 0) <= len(session_data["openaiMessages"])
    ]
    
    save_session(session_id, session_data)
    print(f"[TRUNCATE] after: {len(session_data['events'])} events, {len(session_data['openaiMessages'])} openaiMessages")
    
    # 3.5 同步更新 events.jsonl 文件
    from agents.core.session_backend_jsonl import JsonlSessionBackend
    backend = JsonlSessionBackend()
    backend.truncate(session_id, truncate_at)
    print(f"[TRUNCATE] truncated events.jsonl at seq={truncate_at}")
    
    # 4. 同步更新内存中的 session（如果活跃）
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
    session_data = load_session(session_id)
    if session_data is None:
        raise HTTPException(status_code=404, detail="Session not found")
    
    turn_boundaries = session_data.get("turnBoundaries", [])
    print(f"[REWIND] turnBoundaries: {turn_boundaries}")
    
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
    print(f"[REWIND] target_turn={target_turn}, message_count={message_count}, before={len(messages)}")
    session_data["openaiMessages"] = messages[:message_count]
    
    checkpoint_count = target_boundary.get("checkpoint_count", 0)
    checkpoint_store = session_data.get("checkpointStore", {})
    snapshots = checkpoint_store.get("snapshots", [])
    checkpoint_store["snapshots"] = snapshots[:checkpoint_count]
    session_data["checkpointStore"] = checkpoint_store
    
    session_data["turnBoundaries"] = [b for b in turn_boundaries if b.get("turn") <= target_turn]
    
    save_session(session_id, session_data)
    print(f"[REWIND] after: {len(session_data['openaiMessages'])} messages")
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
async def api_fork_session(session_id: str, data: Optional[ForkRequest] = None) -> dict[str, Any]:
    """原子 Fork：从指定位置切割，创建新 session
    
    支持两种模式：
    1. at_seq: 从指定 seq 切割（推荐）
    2. keep_user_messages: 保留前 N 条用户消息（向后兼容）
    """
    print(f"[FORK] source session_id={session_id}, data={data}")
    
    # 先尝试从session_manager获取session
    from agents.session_manager import get_session_manager
    sm = get_session_manager()
    agent = sm.get_agent(session_id)
    session = sm.get(session_id)
    
    if agent and session:
        # Session在内存中，先保存
        print(f"[FORK] Session {session_id} found in memory, saving...")
        await agent.save()
    
    session_data = load_session(session_id)
    if session_data is None:
        raise HTTPException(status_code=404, detail="Session not found")
    
    original_name = session_data.get("metadata", {}).get("name", "")
    events = session_data.get("events", [])
    print(f"[FORK] original: name={original_name}, events={len(events)}")
    
    new_session_id = uuid.uuid4().hex[:8]
    new_session_data = copy.deepcopy(session_data)
    
    new_session_data["metadata"] = new_session_data.get("metadata", {}).copy()
    new_session_data["metadata"]["id"] = new_session_id
    new_session_data["metadata"]["startTime"] = time.strftime("%Y-%m-%dT%H:%M:%SZ")
    
    # 如果没有 name，用 session_id 作为 base_name
    base_name = original_name
    if not base_name:
        base_name = session_id
    
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
    new_session_data["metadata"]["name"] = fork_name
    
    # 原子切割：根据 at_seq 或 keep_user_messages
    if data and data.at_seq is not None:
        # 模式 1：从指定 seq 切割
        truncate_at = data.at_seq
        print(f"[FORK] truncating events at seq={truncate_at}")
        new_session_data["events"] = events[:truncate_at]
    elif data and data.keep_user_messages is not None:
        # 模式 2：保留前 N 条用户消息
        user_count = 0
        truncate_at = len(events)
        for i, event in enumerate(events):
            if event.get("type") == "user_message":
                user_count += 1
                if user_count >= data.keep_user_messages:
                    # 找到第N个user_message后，继续找下一个非user_message的位置
                    for j in range(i + 1, len(events)):
                        if events[j].get("type") == "user_message":
                            truncate_at = j
                            break
                    else:
                        # 没有更多user_message，保留到末尾
                        truncate_at = len(events)
                    break
        print(f"[FORK] truncating events to keep {data.keep_user_messages} user messages, truncate_at={truncate_at}")
        new_session_data["events"] = events[:truncate_at]
    
    # 同步更新 openaiMessages（向后兼容）
    if "openaiMessages" in new_session_data:
        openai_messages = new_session_data["openaiMessages"]
        if data and data.keep_user_messages is not None:
            user_count = 0
            truncate_at = len(openai_messages)
            for i, msg in enumerate(openai_messages):
                if msg.get("role") == "user":
                    if user_count >= data.keep_user_messages:
                        truncate_at = i
                        break
                    user_count += 1
            new_session_data["openaiMessages"] = openai_messages[:truncate_at]
    
    save_session(new_session_id, new_session_data)
    
    # 复制 events.jsonl 文件
    try:
        from agents.core.session import session_dir
        import shutil
        source_events = session_dir() / f"{session_id}.events.jsonl"
        if source_events.exists():
            target_events = session_dir() / f"{new_session_id}.events.jsonl"
            # 读取并截断events
            truncated_events = new_session_data.get("events", [])
            with target_events.open("w", encoding="utf-8") as f:
                import json
                for event in truncated_events:
                    f.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
            print(f"[FORK] copied events: {source_events} -> {target_events} ({len(truncated_events)} events)")
    except Exception as e:
        print(f"[FORK] failed to copy events: {e}")
    
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
    
    session = Session.load_from_jsonl(session_id)
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
