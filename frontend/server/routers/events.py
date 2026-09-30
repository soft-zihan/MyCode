"""权限/提问响应 router。

SSE 事件流端点已删除（零消费者——前端实时流走 WebSocket，且旧实现对流式事件
缺 seq 字段存在 KeyError）；此处仅保留权限与提问的响应 API。
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter(tags=["events"])


class PermissionResponseData(BaseModel):
    rpc_id: str
    allowed: bool
    session_id: str
    feedback: str = ""
    choice: str = ""


class QuestionResponseData(BaseModel):
    request_id: str
    answer: str
    session_id: str


@router.post("/api/events/respond")
async def api_respond_permission(data: PermissionResponseData):
    """响应权限请求。"""
    from agents.session_manager import get_session_manager
    
    sm = get_session_manager()
    agent = sm.get_agent(data.session_id)
    if not agent:
        return {"success": False, "message": "Session not active"}
    
    # Use set_permission_response to work with the permission gate
    agent.set_permission_response(data.rpc_id, data.allowed, data.feedback, data.choice)
    return {"success": True}


@router.post("/api/events/question-respond")
async def api_respond_question(data: QuestionResponseData):
    """响应用户提问。"""
    from agents.session_manager import get_session_manager
    
    sm = get_session_manager()
    agent = sm.get_agent(data.session_id)
    if not agent:
        return {"success": False, "message": "Session not active"}
    
    agent.session.question_responses[data.request_id] = {"answer": data.answer}
    return {"success": True}


@router.get("/api/tasks/{session_id}")
async def api_get_tasks(session_id: str):
    """获取会话任务清单。"""
    from agents.tools.task_store import list_todos

    items = list_todos(session_id)
    return {"tasks": [item.to_dict() for item in items]}
