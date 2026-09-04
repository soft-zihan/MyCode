"""Chat and streaming APIs."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from agents.core.session import load_session, save_session

router = APIRouter(tags=["chat"])

project_root = Path(__file__).parent.parent.parent.parent


class ChatMessage(BaseModel):
    message: str
    session_id: Optional[str] = None
    context_files: Optional[list[str]] = None
    agent: Optional[str] = None
    model: Optional[str] = None
    permission_mode: Optional[str] = None
    cwd: Optional[str] = None


@router.post("/api/chat")
async def api_chat(data: ChatMessage) -> dict[str, Any]:
    try:
        from agents.agent import Agent
        from agents.config import load_config
        from agents.service import AgentService
        
        config = load_config()
        
        model_name = data.model
        api_key = None
        api_base = None
        
        if model_name:
            for endpoint in config.endpoints.values():
                if endpoint.model == model_name:
                    api_key = endpoint.api_key
                    api_base = endpoint.base_url
                    break
        
        if not api_key and config.endpoints:
            first_endpoint = next(iter(config.endpoints.values()))
            model_name = first_endpoint.model
            api_key = first_endpoint.api_key
            api_base = first_endpoint.base_url
        
        agent = Agent(
            model=model_name,
            api_key=api_key,
            api_base=api_base,
        )
        svc = AgentService(agent)
        
        context = ""
        if data.context_files:
            for item_path in data.context_files:
                try:
                    full_path = project_root / item_path
                    if full_path.exists():
                        if full_path.is_dir():
                            structure = f"\n\n--- {item_path}/ (directory structure) ---\n"
                            try:
                                entries = sorted(full_path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
                                for entry in entries[:50]:
                                    prefix = "📁 " if entry.is_dir() else "📄 "
                                    structure += f"{prefix}{entry.name}\n"
                                if len(entries) > 50:
                                    structure += f"... and {len(entries) - 50} more entries\n"
                            except PermissionError:
                                structure += "(Permission denied)\n"
                            context += structure
                        else:
                            content = full_path.read_text(encoding="utf-8")
                            context += f"\n\n--- {item_path} ---\n{content}"
                except Exception as e:
                    context += f"\n\n--- {item_path} ---\nError reading: {e}"
        
        full_message = data.message
        if context:
            full_message = f"{data.message}\n\nContext files:{context}"
        
        await svc.chat(full_message)
        
        return {
            "response": svc.last_response or '',
            "session_id": svc.session_id,
        }
    except Exception as e:
        import traceback
        return {
            "response": f"Error processing message: {str(e)}\n\n{traceback.format_exc()}",
            "session_id": data.session_id or "error-session",
        }


@router.post("/api/chat/stream")
async def api_chat_stream(data: ChatMessage) -> dict[str, Any]:
    """Start a chat turn. Events are pushed via WebSocket.
    
    This endpoint:
    1. Creates or restores a session
    2. Starts agent.chat() as a background task
    3. Returns the session_id immediately
    4. Events are pushed to the client via WebSocket (/ws/events)
    """
    from agents.session_manager import get_session_manager
    import logging
    import asyncio
    logger = logging.getLogger(__name__)
    
    logger.info(f"[DEBUG] /api/chat/stream received session_id: {data.session_id}")
    
    try:
        original_cwd = os.getcwd()
        if data.cwd:
            os.chdir(data.cwd)
        
        sm = get_session_manager()
        
        agent, session = None, None
        is_new_session = False
        if data.session_id:
            logger.info(f"[DEBUG] Trying to restore session: {data.session_id}")
            result = sm.restore(data.session_id, data.cwd)
            if result:
                agent, session = result
                logger.info(f"[DEBUG] Session restored successfully: {session.id}")
            else:
                logger.warning(f"[DEBUG] Failed to restore session: {data.session_id}")
        
        if not agent:
            logger.info(f"[DEBUG] Creating new session (requested: {data.session_id})")
            agent, session = sm.create(
                data.model,
                data.agent,
                data.permission_mode,
                data.cwd,
            )
            is_new_session = True
            logger.info(f"[DEBUG] New session created: {session.id}")
            
            # 保存 cwd 到 session 的 projections
            if data.cwd:
                session._projections["cwd"] = data.cwd
            
            # Register project
            if data.cwd:
                from agents.core.project import register_project
                register_project(data.cwd)
        
        # Build context
        context = ""
        if data.context_files:
            for item_path in data.context_files:
                try:
                    full_path = project_root / item_path
                    if full_path.exists():
                        if full_path.is_dir():
                            structure = f"\n\n--- {item_path}/ (directory structure) ---\n"
                            try:
                                entries = sorted(full_path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
                                for entry in entries[:50]:
                                    prefix = "📁 " if entry.is_dir() else "📄 "
                                    structure += f"{prefix}{entry.name}\n"
                                if len(entries) > 50:
                                    structure += f"... and {len(entries) - 50} more entries\n"
                            except PermissionError:
                                structure += "(Permission denied)\n"
                            context += structure
                        else:
                            content = full_path.read_text(encoding="utf-8")
                            context += f"\n\n--- {item_path} ---\n{content}"
                except Exception as e:
                    context += f"\n\n--- {item_path} ---\nError reading: {e}"
        
        full_message = data.message
        if context:
            full_message = f"{data.message}\n\nContext files:{context}"
        
        # Send session/created event via WebSocket if new session
        if is_new_session:
            from routers.websocket import broadcast_event
            broadcast_event({
                "type": "session/created",
                "session_id": session.id,
                "cwd": data.cwd,
                "time": int(asyncio.get_event_loop().time() * 1000),
            })
            
            # Start title generation in background
            async def generate_title():
                logger.info(f"[TITLE] Starting title generation for session {session.id}")
                try:
                    from frontend.server.routers.sessions import generate_session_title
                    title_name = await generate_session_title(data.message)
                    logger.info(f"[TITLE] Generated name: {title_name}")
                    # Broadcast title via WebSocket
                    from routers.websocket import broadcast_event
                    broadcast_event({
                        "type": "session/title",
                        "session_id": session.id,
                        "title": title_name,
                        "time": int(asyncio.get_event_loop().time() * 1000),
                    })
                    logger.info(f"[TITLE] Title broadcasted for session {session.id}")
                except Exception as e:
                    import traceback
                    logger.error(f"[TITLE] Title generation failed: {e}")
                    logger.error(f"[TITLE] Traceback: {traceback.format_exc()}")
            
            asyncio.create_task(generate_title())
            logger.info(f"[TITLE] Title generation task created for session {session.id}")
        
        # Start agent chat in background
        async def run_chat():
            print(f"[DEBUG] run_chat: STARTED for session {session.id}")
            try:
                print(f"[DEBUG] run_chat: BEFORE agent.chat - agent.session_id = {agent.session_id}, agent.session.id = {agent.session.id}, id(agent.session) = {id(agent.session)}, session.id = {session.id}, id(session) = {id(session)}")
                await agent.chat(full_message)
                print(f"[DEBUG] run_chat: AFTER agent.chat - agent.session_id = {agent.session_id}, agent.session.id = {agent.session.id}")
            except asyncio.CancelledError:
                print(f"[DEBUG] run_chat: Task cancelled for session {session.id}")
                raise
            except Exception as e:
                session.append("error", {"message": str(e)})
            finally:
                await agent.save()
                if data.cwd:
                    os.chdir(original_cwd)
        
        chat_task = asyncio.create_task(run_chat())
        print(f"[DEBUG] chat_task created: {chat_task}")
        sm.register_chat_task(session.id, chat_task)
        
        # 给任务一个机会开始执行
        await asyncio.sleep(0)
        print(f"[DEBUG] after sleep(0), chat_task done: {chat_task.done()}, cancelled: {chat_task.cancelled()}")
        
        # Return session_id immediately
        print(f"[DEBUG] Returning session_id: {session.id}")
        return {
            "session_id": session.id,
            "is_new_session": is_new_session,
        }
    
    except Exception as e:
        import traceback
        error_msg = f"Error processing message: {str(e)}\n\n{traceback.format_exc()}"
        logger.error(error_msg)
        return {
            "session_id": data.session_id or "error-session",
            "error": error_msg,
        }


class RevertRequest(BaseModel):
    file_path: str
    old_content: str


@router.post("/api/revert")
def api_revert_file(data: RevertRequest) -> dict[str, Any]:
    try:
        from agents.tools import _resolve_tool_path
        target = _resolve_tool_path(data.file_path, must_exist=False)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(data.old_content)
        return {"success": True, "file_path": data.file_path}
    except Exception as e:
        from fastapi import HTTPException
        raise HTTPException(status_code=500, detail=str(e))
