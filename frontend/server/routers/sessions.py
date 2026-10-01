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

from agents.config import DEFAULT_CONTEXT_WINDOW
from agents.core.workspace import workspace_scope
from agents.core.session import list_sessions, delete_session, session_dir

router = APIRouter(tags=["sessions"])

def _get_live_agent(session_id: str):
    """内存中的 live agent（未加载返回 None）。session_manager 是唯一激活注册表。"""
    from agents.session_manager import get_session_manager
    return get_session_manager().get_agent(session_id)


async def _ensure_agent(session_id: str):
    """获取或 hydrate agent（走 SessionBackend 加载事件；session 不存在返回 None）。"""
    from agents.session_manager import get_session_manager
    result = await get_session_manager().restore(session_id)
    return result[0] if result else None


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
    context_files: Optional[list[str]] = None


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
    # U2：派生会话（子代理/eval）已落盘但不进用户会话列表；钻取走 GET /api/sessions/{sub_id}
    from agents.core.session import is_derived_session_meta
    sessions = [s for s in list_sessions() if not is_derived_session_meta(s)]
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
    from agents.session_manager import get_session_manager
    session = get_session_manager().get_or_load(session_id)
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
    
    session = get_session_manager().get_or_load(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    
    session.append("session/title", {"title": request["name"]})
    print(f"[PUT] Renamed session {session_id} -> {request['name']}")
    return {"success": True, "message": "Session renamed"}


@router.get("/api/sessions/{session_id}/projections")
def api_get_session_projections(session_id: str) -> dict[str, Any]:
    """快速获取 session 投影值（title, updatedAt, cwd, running）。"""
    from agents.session_manager import get_session_manager
    session = get_session_manager().get_or_load(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return session.projections


@router.get("/api/sessions/{session_id}/summary")
def api_session_summary(session_id: str) -> dict[str, Any]:
    result: dict[str, Any] = {}

    from agents.core.session import get_session_backend
    backend = get_session_backend()
    if not backend.session_exists(session_id):
        raise HTTPException(status_code=404, detail="Session not found")

    from agents.session_manager import get_session_manager
    session = get_session_manager().get_or_load(session_id)
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

    latest_stats = backend.get_latest_event(session_id, "stats") or {}

    result["stats"] = {
        "input_tokens": latest_stats.get("input_tokens", 0),
        "output_tokens": latest_stats.get("output_tokens", 0),
        "cached_tokens": latest_stats.get("cached_tokens", 0),
        "cache_hit_rate": latest_stats.get("cache_hit_rate", 0.0),
        "total_input_tokens": latest_stats.get("total_input_tokens", 0),
        "total_output_tokens": latest_stats.get("total_output_tokens", 0),
        "total_cached_tokens": latest_stats.get("total_cached_tokens", 0),
        "context_window": latest_stats.get("context_window", DEFAULT_CONTEXT_WINDOW),
        "effective_window": latest_stats.get("effective_window", DEFAULT_CONTEXT_WINDOW),
        "estimated_context_tokens": latest_stats.get("estimated_context_tokens", 0),
    }

    result["breakdown"] = _compute_breakdown_from_stats(latest_stats, None)

    return result


def _compute_breakdown_from_stats(latest_stats: dict, agent: Any) -> dict[str, Any]:
    actual_input_tokens = latest_stats.get("input_tokens", 0)
    system_chars = latest_stats.get("system_chars", 0)
    user_chars = latest_stats.get("user_chars", 0)
    assistant_chars = latest_stats.get("assistant_chars", 0)
    tool_result_chars = latest_stats.get("tool_result_chars", 0)

    system_base_chars = latest_stats.get("system_base_chars", 0)
    system_claude_md_chars = latest_stats.get("system_claude_md_chars", 0)
    system_skills_chars = latest_stats.get("system_skills_chars", 0)
    system_wiki_chars = latest_stats.get("system_wiki_chars", 0)
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
        wiki_tokens = int((system_wiki_chars / 4) * scale)
        plan_mode_chars = latest_stats.get("plan_mode_chars", 0)
        plan_mode_tokens = int((plan_mode_chars / 4) * scale)
    else:
        user_tokens = user_chars // 4
        assistant_tokens = assistant_chars // 4
        tool_tokens = tool_result_chars // 4
        base_prompt_tokens = (system_base_chars + system_workspace_chars) // 4
        claude_md_tokens = system_claude_md_chars // 4
        skills_tokens = system_skills_chars // 4
        wiki_tokens = system_wiki_chars // 4
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
        "wiki_tokens": wiki_tokens,
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
    
    agent = await _ensure_agent(session_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="Session not found")
    try:
        folded = await agent.compact()
    except Exception as e:
        return {"success": False, "message": str(e)}

    if not folded:
        return {"success": True, "folded": False, "message": "No compaction needed"}

    from frontend.server.routers.websocket import broadcast_event
    broadcast_event(
        {
            "type": "context/compacted",
            "session_id": session_id,
            "message": "上下文已压缩",
            "estimated_context_tokens": agent.estimated_context_tokens,
            "effective_window": agent.effective_window,
            "context_window": agent.context_window,
        },
        target_session_id=session_id,
    )
    return {"success": True, "folded": True, "message": "Context compacted"}


# ── 统一回退 API（对话 + 文件原子回退，三阶段） ──


@router.post("/api/sessions/{session_id}/rewind/stage")
async def api_rewind_stage(session_id: str, data: RewindStageRequest) -> dict[str, Any]:
    """Stage：生成回退计划（文件 diff + 对话截断预览），不修改任何状态。"""
    from agents.core.rewind_service import get_rewind_service
    try:
        agent = _get_live_agent(session_id)
        if agent is not None:
            plan_obj = await get_rewind_service().stage(
                session_id,
                turns=data.turns,
                keep_user_messages=data.keep_user_messages,
                session=agent.session,
                workspace=str(agent.workspace),
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
        agent = _get_live_agent(session_id)
        if agent is not None:
            result = await get_rewind_service().commit(data.plan_id, session=agent.session)
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
        agent = _get_live_agent(session_id)
        if agent is not None:
            message = await agent.rewind_turns(data.turns)
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
    agent = _get_live_agent(session_id)
    if agent is not None:
        return {"permission_mode": agent.permission_mode}
    return {"permission_mode": "default"}


@router.put("/api/sessions/{session_id}/permission-mode")
async def api_update_permission_mode(session_id: str, data: PermissionModeRequest) -> dict[str, Any]:
    agent = await _ensure_agent(session_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="Session not found")
    agent.set_permission_mode(data.mode)
    return {"success": True, "permission_mode": data.mode}


def _zero_token_breakdown() -> dict[str, Any]:
    return {
        "base_prompt_tokens": 0, "claude_md_tokens": 0, "skills_tokens": 0,
        "wiki_tokens": 0,
        "tools_tokens": 0, "builtin_tool_count": 0, "mcp_tool_count": 0,
        "messages_tokens": 0, "message_count": 0,
        "user_tokens": 0, "assistant_tokens": 0, "tool_tokens": 0,
        "total_tokens": 0,
        "is_plan_mode": False, "plan_mode_tokens": 0,
    }


def _rebuild_breakdown_chars(backend: Any, session_id: str) -> dict[str, int]:
    """stats 缺少 breakdown 时从事件流重建各段字符数（保留原估算规则）。"""
    system_chars = 0
    user_chars = 0
    assistant_chars = 0
    tool_result_chars = 0
    message_count = 0

    for event in backend.load_all_events(session_id):
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

    # 估算 system prompt 各部分（使用默认值）
    if system_chars == 0:
        system_chars = 6000  # 默认系统提示词大小
    return {
        "system_chars": system_chars,
        "user_chars": user_chars,
        "assistant_chars": assistant_chars,
        "tool_result_chars": tool_result_chars,
        "message_count": message_count,
        "system_base_chars": int(system_chars * 0.55),       # 基础提示词占 55%
        "system_claude_md_chars": int(system_chars * 0.2),   # CLAUDE.md 占 20%
        "system_skills_chars": int(system_chars * 0.1),      # Skills 占 10%
        "system_wiki_chars": int(system_chars * 0.05),       # Wiki 占 5%
        "system_workspace_chars": int(system_chars * 0.05),  # Workspace 占 5%
    }


def _mcp_tools_chars(latest_stats: dict[str, Any]) -> tuple[int, int, int]:
    """tools 定义字符数与 builtin/mcp 数量；MCP manager 不可用时退回 stats。"""
    try:
        from frontend.server.mcp_manager import global_mcp_manager
        tool_defs = global_mcp_manager.get_tool_definitions()
        tools_json = json.dumps([{
            'name': t.get('name', ''),
            'description': t.get('description', ''),
            'parameters': t.get('parameters', {}),
        } for t in tool_defs], ensure_ascii=False)
        return len(tools_json), 0, len(tool_defs)
    except Exception:
        return 0, latest_stats.get("tool_count", 0), 0


def _scale_chars_to_tokens(chars: int, scale: Optional[float]) -> int:
    """有实际 token 数时按缩放比换算 chars→tokens；否则按 4 chars≈1 token 估算。"""
    if scale is not None:
        return int((chars / 4) * scale)
    return chars // 4


def _distribute_tool_result_tokens(
    by_name_chars: dict[str, int], tool_tokens: int
) -> dict[str, int]:
    """按各工具结果字符占比分摊 tool 结果总 token。"""
    result: dict[str, int] = {}
    if by_name_chars and tool_tokens > 0:
        total_tool_chars = sum(by_name_chars.values())
        if total_tool_chars > 0:
            for tool_name, chars in by_name_chars.items():
                result[tool_name] = int(tool_tokens * (chars / total_tool_chars))
    return result


def _resolve_breakdown_chars(
    backend: Any, session_id: str, latest_stats: dict[str, Any]
) -> dict[str, int]:
    """优先取 stats 中的 breakdown 字符数；缺失则从事件流重建并补估算段。"""
    system_chars = latest_stats.get("system_chars", 0)
    user_chars = latest_stats.get("user_chars", 0)
    assistant_chars = latest_stats.get("assistant_chars", 0)
    tool_result_chars = latest_stats.get("tool_result_chars", 0)

    has_breakdown = system_chars > 0 or user_chars > 0 or assistant_chars > 0 or tool_result_chars > 0
    if not has_breakdown:
        return _rebuild_breakdown_chars(backend, session_id)
    return {
        "system_chars": system_chars,
        "user_chars": user_chars,
        "assistant_chars": assistant_chars,
        "tool_result_chars": tool_result_chars,
        "message_count": latest_stats.get("msg_count", 0),
        "system_base_chars": latest_stats.get("system_base_chars", 0),
        "system_claude_md_chars": latest_stats.get("system_claude_md_chars", 0),
        "system_skills_chars": latest_stats.get("system_skills_chars", 0),
        "system_wiki_chars": latest_stats.get("system_wiki_chars", 0),
        "system_workspace_chars": latest_stats.get("system_workspace_chars", 0),
    }


@router.get("/api/sessions/{session_id}/token-breakdown")
async def api_get_token_breakdown(session_id: str) -> dict[str, Any]:
    """获取实际发给模型的 token 分解
    
    从事件日志最新 stats 事件读取细粒度 breakdown 数据（走 backend，两后端一致）
    """
    from agents.core.session import get_session_backend
    backend = get_session_backend()

    if not backend.session_exists(session_id):
        return _zero_token_breakdown()

    # 从最新的 stats 事件读取
    latest_stats = backend.get_latest_event(session_id, "stats") or {}
    if not latest_stats:
        return _zero_token_breakdown()

    actual_input_tokens = latest_stats.get("last_input_token_count", 0)
    seg = _resolve_breakdown_chars(backend, session_id, latest_stats)

    # 计算 tools 定义的字符数（用于缩放计算）
    tools_chars, builtin_tool_count, mcp_tool_count = _mcp_tools_chars(latest_stats)

    # total_chars 包含 tools
    total_chars = (seg["system_chars"] + tools_chars + seg["user_chars"]
                   + seg["assistant_chars"] + seg["tool_result_chars"])
    scale: Optional[float] = None
    if actual_input_tokens > 0 and total_chars > 0:
        scale = actual_input_tokens / (total_chars / 4)

    user_tokens = _scale_chars_to_tokens(seg["user_chars"], scale)
    assistant_tokens = _scale_chars_to_tokens(seg["assistant_chars"], scale)
    tool_tokens = _scale_chars_to_tokens(seg["tool_result_chars"], scale)
    tools_definition_tokens = _scale_chars_to_tokens(tools_chars, scale)
    messages_tokens = user_tokens + assistant_tokens + tool_tokens

    # 按工具名拆分的结果 token
    tool_result_by_name = _distribute_tool_result_tokens(
        latest_stats.get("tool_result_by_name", {}), tool_tokens)

    return {
        "base_prompt_tokens": _scale_chars_to_tokens(seg["system_base_chars"] + seg["system_workspace_chars"], scale),
        "claude_md_tokens": _scale_chars_to_tokens(seg["system_claude_md_chars"], scale),
        "skills_tokens": _scale_chars_to_tokens(seg["system_skills_chars"], scale),
        "wiki_tokens": _scale_chars_to_tokens(seg["system_wiki_chars"], scale),
        "tools_tokens": tools_definition_tokens,
        "builtin_tool_count": builtin_tool_count,
        "mcp_tool_count": mcp_tool_count,
        "messages_tokens": messages_tokens,
        "message_count": seg["message_count"],
        "user_tokens": user_tokens,
        "assistant_tokens": assistant_tokens,
        "tool_tokens": tool_tokens,
        "tool_result_by_name": tool_result_by_name,
        "total_tokens": actual_input_tokens,
        "is_plan_mode": latest_stats.get("is_plan_mode", False),
        # Plan mode
        "plan_mode_tokens": _scale_chars_to_tokens(latest_stats.get("plan_mode_chars", 0), scale),
    }


@router.post("/api/sessions/{session_id}/steer")
async def api_steer_session(session_id: str, data: SteerRequest) -> dict[str, Any]:
    agent = _get_live_agent(session_id)
    # U1：非运行中直接拒绝——前端凭 success=false 退回普通发送，杜绝"消息被吃了"的假 steer
    if agent is None or not agent.is_processing:
        return {"success": False, "message": "Session not active"}
    from frontend.server.routers.chat import build_message_with_context
    message = build_message_with_context(data.message, data.context_files)
    # steer/queued 落盘 + WS 广播，前端乐观 UI 凭事件流转状态（queued→delivered）
    agent.session.append("steer/queued", {"content": message[:200]})
    await agent.steer(message)
    return {"success": True, "message": "Steer message queued"}


@router.post("/api/sessions/{session_id}/subagents/{sub_session_id}/background")
async def api_background_subagent(session_id: str, sub_session_id: str) -> dict[str, Any]:
    """U3a：把正在前台阻塞的子代理 run 转为后台（v2 job.background 语义）。

    标记后前台 block 立即以 state="backgrounded" 返回、主会话可继续对话；
    run 不中断，完成后经 subagent/completed synthetic 事件注入父会话。
    """
    from agents.core.job_registry import get_job

    job = get_job(sub_session_id)
    if job is None or job.done.is_set():
        return {"success": False, "message": "Sub-agent is not running"}
    if job.parent_session_id != session_id:
        raise HTTPException(status_code=404, detail="Not a sub-agent of this session")
    job.mark_background()
    return {"success": True, "message": "Sub-agent moved to background"}


@router.post("/api/sessions/{session_id}/subagents/{sub_session_id}/cancel")
async def api_cancel_subagent(session_id: str, sub_session_id: str) -> dict[str, Any]:
    """U4：硬中止后台子代理（软 abort + 硬 task.cancel 双通道）。

    子会话已落盘，取消后仍可通过 agent 工具 session_id 续跑；
    取消通知经 subagent/completed（state="cancelled"）幂等注入父会话。
    """
    from agents.core.job_registry import get_job

    job = get_job(sub_session_id)
    if job is None or job.done.is_set():
        return {"success": False, "message": "Sub-agent is not running"}
    if job.parent_session_id != session_id:
        raise HTTPException(status_code=404, detail="Not a sub-agent of this session")
    job.request_cancel()
    return {"success": True, "message": "Sub-agent cancel requested"}


def _resolve_fork_name(base_name: str) -> str:
    """生成不与现有会话重名的 fork 名称。"""
    all_sessions = list_sessions()
    fork_num = 1
    while True:
        fork_name = f"{base_name}(fork {fork_num})"
        if not any(s.get("name") == fork_name for s in all_sessions):
            return fork_name
        fork_num += 1


def _truncate_fork_events(events: list[dict], data: Optional["ForkRequest"]) -> list[dict]:
    """按 fork 参数截断事件：at_seq 优先，其次 keep_user_messages。"""
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
    return truncated_events


def _build_fork_projcache(new_session_id: str, copied_events: list[dict], cwd: Optional[str]) -> None:
    """为 fork 出的新会话立即构建 projcache，前端列表无需等回放即可读投影。"""
    try:
        from agents.core.session_projection_cache import (
            restore_projections,
            SessionHeader,
            FORMAT_VERSION,
        )
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


@router.post("/api/sessions/{session_id}/fork")
async def api_fork_session(session_id: str, data: Optional[ForkRequest] = None) -> dict[str, Any]:
    """原子 Fork：从指定位置切割，创建新 session
    
    支持两种模式：
    1. at_seq: 从指定 seq 切割（推荐）
    2. keep_user_messages: 保留前 N 条用户消息（向后兼容）
    """
    print(f"[FORK] source session_id={session_id}, data={data}")
    
    from agents.session_manager import get_session_manager
    from agents.core.session import Session, get_session_backend
    
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
    if not original_title and session is None:
        # BC-26：仅非活会话允许磁盘加载（load_from_events 带修复写副作用）
        loaded = Session.load_from_events(session_id)
        if loaded is not None:
            original_title = loaded.projections.get("title")
    print(f"[FORK] original: title={original_title}, events={len(events)}")
    
    new_session_id = uuid.uuid4().hex[:8]
    
    base_name = original_title or session_id
    if "(fork " in base_name:
        base_name = base_name.rsplit("(fork ", 1)[0]
    fork_name = _resolve_fork_name(base_name)
    print(f"[FORK] new: id={new_session_id}, name={fork_name}")
    
    truncated_events = _truncate_fork_events(events, data)
    
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
    
    # 写入必须走全局 backend：硬编码 JsonlSessionBackend 曾导致 sqlite 下
    # fork 出的会话读不到（读走 sqlite、写落 JSONL，D5 读写分裂）
    for event in copied_events:
        backend.append(new_session_id, event)
    print(f"[FORK] wrote {len(copied_events)} events to {new_session_id}")
    
    cwd = session.projections.get("cwd") if session else None
    if not cwd and loaded is not None:
        cwd = loaded.projections.get("cwd")
    _build_fork_projcache(new_session_id, copied_events, cwd)
    
    return {
        "success": True,
        "new_session_id": new_session_id,
        "fork_name": fork_name,
        "message": f"Session forked to {new_session_id}"
    }


def _session_workspace(session_id: str) -> Path:
    """解析会话的 workspace：活跃会话取 agent.workspace，否则读 projcache 的 cwd。"""
    agent = _get_live_agent(session_id)
    if agent is not None:
        return Path(agent.workspace)
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


@router.get("/api/sessions/{session_id}/compression-stats")
def api_compression_stats(session_id: str) -> dict[str, Any]:
    from agents.core.session import get_session_backend
    backend = get_session_backend()
    if not backend.session_exists(session_id):
        raise HTTPException(status_code=404, detail="Session not found")

    from agents.core.context_events import collect_hidden_seqs

    token_count = 0
    context_window = DEFAULT_CONTEXT_WINDOW
    effective_window = DEFAULT_CONTEXT_WINDOW
    tool_fold_count = 0
    session_fold_count = 0
    last_fold_time: int | None = None
    folded_memories: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = backend.load_all_events(session_id)

    hidden_seqs = collect_hidden_seqs(events)
    for event in events:
        event_type = event.get("type", "")
        if event_type == "stats":
            token_count = int(event.get("estimated_context_tokens") or event.get("last_total_token_count") or 0)
            context_window = int(event.get("context_window") or context_window)
            effective_window = int(event.get("effective_window") or effective_window)
        elif event_type == "tool_folded":
            tool_fold_count += 1
            last_fold_time = event.get("time", last_fold_time)
        elif event_type == "session_folded":
            session_fold_count += 1
            last_fold_time = event.get("time", last_fold_time)
            if event.get("seq") in hidden_seqs:
                continue
            folded_memories.append({
                "time": event.get("time", ""),
                "trigger": event.get("trigger", "auto"),
                "summary": event.get("summary", ""),
                "session_notes": event.get("session_notes", ""),
                "project_knowledge": event.get("project_knowledge", ""),
            })

    utilization = token_count / effective_window if effective_window else 0
    return {
        "utilization": round(utilization, 3),
        "token_count": token_count,
        "effective_window": effective_window,
        "context_window": context_window,
        "tool_fold": {"triggered": tool_fold_count},
        "session_fold": {"triggered": session_fold_count},
        "total_folds": {
            "triggered": tool_fold_count + session_fold_count,
            "last_fold_time": last_fold_time,
        },
        "folded_memories": folded_memories,
    }


@router.get("/api/sessions/{session_id}/context-store")
def api_context_store(session_id: str) -> dict[str, Any]:
    """返回被 events_hidden 隐藏的事件信息。"""
    from agents.core.context_events import collect_hidden_seqs

    def _content_size(event: dict[str, Any]) -> int:
        content = event.get("content")
        if isinstance(content, str):
            return len(content)
        if isinstance(content, (dict, list)):
            return len(json.dumps(content, ensure_ascii=False, default=str))
        return 0

    def _build_payload(events: list[dict[str, Any]]) -> dict[str, Any]:
        hidden_seqs = collect_hidden_seqs(events)
        entries = []
        for event in events:
            seq = event.get("seq")
            if not isinstance(seq, int) or seq not in hidden_seqs:
                continue
            entries.append({
                "seq": seq,
                "type": event.get("type"),
                "call_id": event.get("call_id", ""),
                "content_size": _content_size(event),
            })
        return {
            "entries": entries,
            "total_entries": len(entries),
            "total_raw_size": sum(entry["content_size"] for entry in entries),
            "active_entries": len([entry for entry in entries if entry.get("type") == "tool_result_msg"]),
        }

    from agents.core.session import get_session_backend
    events = get_session_backend().load_all_events(session_id)
    if not events:
        raise HTTPException(status_code=404, detail="Session not found")
    return _build_payload(list(events))


def _resolve_session_cwd(session_id: str) -> Optional[str]:
    """会话 cwd：优先活 agent 工作区，其次会话元数据。"""
    agent = _get_live_agent(session_id)
    cwd = None
    if agent is not None:
        cwd = getattr(agent, 'workspace', None) or getattr(agent, 'cwd', None)

    if not cwd:
        # Try to get cwd from session metadata
        for s in list_sessions():
            if s.get("id") == session_id:
                cwd = s.get("cwd")
                break
    return cwd


def _is_snapshot_internal_path(path: str) -> bool:
    return path.startswith(".mycode/") or path.startswith("manifests/")


async def _collect_snapshot_file_entries(
    items: Any,
    git_repo: Any,
    old_tree_hash: str,
    new_tree_hash: Optional[str],
    cwd: str,
) -> list[dict[str, Any]]:
    """从 diff/inspection 条目组装文件内容对。

    new_tree_hash 为 None 表示单快照场景：new_content 读当前工作区文件。
    """
    files = []
    for f in items:
        if _is_snapshot_internal_path(f.path):
            continue

        old_content = ""
        new_content = ""
        is_new = f.status == "added"

        # Get old content from first snapshot tree
        if f.status in ("modified", "deleted"):
            old_content = await git_repo.get_file_content(old_tree_hash, f.path)

        # Get new content from last snapshot tree / current working tree
        if f.status in ("added", "modified"):
            if new_tree_hash is not None:
                new_content = await git_repo.get_file_content(new_tree_hash, f.path)
            else:
                file_path = Path(cwd) / f.path
                if file_path.exists():
                    new_content = file_path.read_text(encoding="utf-8", errors="replace")

        files.append({
            "file_path": f.path,
            "is_new": is_new,
            "old_content": old_content,
            "new_content": new_content,
        })
    return files


@router.get("/api/sessions/{session_id}/file-changes")
async def api_session_file_changes(session_id: str) -> dict[str, Any]:
    """Get file changes for a session using git-based snapshots.
    
    Returns a list of file diffs between the first and last snapshot of the session.
    Each file includes path, status (added/modified/deleted), old_content, new_content.
    """
    from agents.core.snapshot_service import SnapshotService
    from agents.core.git_repository import GitRepositoryManager

    cwd = _resolve_session_cwd(session_id)
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
        first_manifest = await snapshot_service.get_manifest(first_snapshot.id)

        if len(snapshots) == 1:
            # Only one snapshot, diff against current state
            inspection = await snapshot_service.inspect(first_snapshot.id)
            files = await _collect_snapshot_file_entries(
                inspection.files, git_repo, first_manifest.tree_hash, None, cwd)
        else:
            # Diff between first and last snapshot
            diffs = await snapshot_service.diff(first_snapshot.id, last_snapshot.id)
            last_manifest = await snapshot_service.get_manifest(last_snapshot.id)
            files = await _collect_snapshot_file_entries(
                diffs, git_repo, first_manifest.tree_hash, last_manifest.tree_hash, cwd)

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
    from agents.session_manager import get_session_manager
    
    # BC-26：前端 gap repair 会高频轮询本端点——活会话必须走内存实例，
    # 否则每次 load_from_events 都对存活 turn 触发 crash-repair 写
    session = get_session_manager().get_or_load(session_id)
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
