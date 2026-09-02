"""Chat and streaming APIs."""

from __future__ import annotations

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
async def api_chat_stream(data: ChatMessage):
    from routers.sessions import get_active_sessions
    
    try:
        from agents.agent import Agent
        from agents.config import load_config
        from agents.service import AgentService
        
        original_cwd = os.getcwd()
        os.chdir(data.cwd)
        
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
        
        if data.agent == "plan":
            permission_mode = "plan"
        else:
            permission_mode = data.permission_mode or "bypassPermissions"
        
        agent = Agent(
            model=model_name,
            api_key=api_key,
            api_base=api_base,
            permission_mode=permission_mode,
        )
        svc = AgentService(agent)
        
        if data.session_id:
            from agents.core.session import load_session
            session_data = load_session(data.session_id)
            if session_data:
                svc.restore(session_data)
                svc.set_session_id(data.session_id)
        
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
        
        # Handle /goal command specially (multi-step process)
        if full_message.lower().startswith("/goal"):
            goal_text = full_message[5:].strip()
            
            # Check if this is a confirmation
            if goal_text.lower() == "confirm" or goal_text.lower() == "yes":
                # Get pending goal from session metadata
                session_data = load_session(session_id) if session_id else None
                pending_goal = session_data.get("pendingGoal") if session_data else None
                if not pending_goal:
                    async def generate_no_goal():
                        yield f"data: {json.dumps({'text': {'content': 'No pending goal to confirm. Use /goal <description> to start.'}})}\n\n"
                        yield f"data: {json.dumps({'done': True, 'session_id': session_id})}\n\n"
                    return StreamingResponse(generate_no_goal(), media_type="text/event-stream")
                
                # Start goal loop
                async def generate_goal_loop():
                    from agents.goal import GoalLoop
                    
                    goal = pending_goal["goal"]
                    criteria = pending_goal["criteria"]
                    
                    # Clear pending goal
                    if session_data:
                        session_data.pop("pendingGoal", None)
                        save_session(session_id, session_data)
                    
                    yield f"data: {json.dumps({'goal_start': {'goal': goal, 'criteria': criteria}})}\n\n"
                    
                    loop = GoalLoop(svc.agent, goal, criteria, max_iterations=10)
                    
                    async def side_query_wrapper(system_prompt: str, user_prompt: str) -> str:
                        # Use the side query from agent
                        sq = svc.agent._build_side_query(max_tokens=2400)
                        if sq:
                            return await sq(system_prompt, user_prompt)
                        return ""
                    
                    loop._side_query = side_query_wrapper
                    
                    # Run iterations
                    for iteration in range(1, loop.max_iterations + 1):
                        if svc.agent._abort_event and svc.agent._abort_event.is_set():
                            yield f"data: {json.dumps({'goal_progress': {'iteration': iteration, 'status': 'aborted'}})}\n\n"
                            break
                        
                        yield f"data: {json.dumps({'goal_progress': {'iteration': iteration, 'max': loop.max_iterations, 'status': 'running'}})}\n\n"
                        
                        # Compose prompt
                        if iteration == 1:
                            criteria_block = "\n".join(f"{i}. {c}" for i, c in enumerate(criteria, 1))
                            prompt = (
                                f"[GOAL MODE] Work autonomously toward this goal:\n{goal}\n\n"
                                f"Success criteria:\n{criteria_block}\n\n"
                                f"Work step by step. Use tools to make real progress, then report "
                                f"what you did and the observable evidence for each criterion."
                            )
                        else:
                            prompt = (
                                f"[GOAL MODE] Not all success criteria are met yet.\n"
                                f"{loop.state.last_feedback}\n\n"
                                f"Continue working toward the goal and report new evidence."
                            )
                        
                        # Run one iteration through normal chat_stream
                        async for event in svc.chat_stream(prompt):
                            # Forward all events to frontend
                            event_type = event.get("type")
                            if event_type == "thinking":
                                yield f"data: {json.dumps({'thinking': {'content': event.get('content', '')}})}\n\n"
                            elif event_type == "text":
                                yield f"data: {json.dumps({'text': {'content': event.get('content', '')}})}\n\n"
                            elif event_type == "tool_call":
                                yield f"data: {json.dumps({'tool_call': {'name': event.get('name', ''), 'input': event.get('input', {})}})}\n\n"
                            elif event_type == "tool_result":
                                yield f"data: {json.dumps({'tool_result': {'name': event.get('name', ''), 'result': event.get('result', ''), 'status': event.get('status', 'ok')}})}\n\n"
                        
                        # Verify
                        verdict = await loop._verify()
                        loop.state.iteration = iteration
                        
                        if verdict["all_met"]:
                            yield f"data: {json.dumps({'goal_complete': {'status': 'achieved', 'iteration': iteration}})}\n\n"
                            break
                        
                        loop.state.last_feedback = verdict["feedback"]
                        yield f"data: {json.dumps({'goal_feedback': {'iteration': iteration, 'feedback': verdict['feedback']}})}\n\n"
                    else:
                        yield f"data: {json.dumps({'goal_complete': {'status': 'budget_exhausted', 'iteration': loop.max_iterations}})}\n\n"
                    
                    yield f"data: {json.dumps({'done': True, 'session_id': svc.session_id})}\n\n"
                
                return StreamingResponse(generate_goal_loop(), media_type="text/event-stream")
            
            # Extract criteria for new goal
            if not goal_text:
                async def generate_usage():
                    yield f"data: {json.dumps({'text': {'content': 'Usage: /goal <goal description>'}})}\n\n"
                    yield f"data: {json.dumps({'done': True, 'session_id': session_id})}\n\n"
                return StreamingResponse(generate_usage(), media_type="text/event-stream")
            
            async def generate_extract_criteria():
                from agents.goal import extract_goal_criteria
                
                side_query = svc.agent._build_side_query(max_tokens=2400)
                if not side_query:
                    yield f"data: {json.dumps({'text': {'content': 'No side-query model configured; cannot extract goal criteria.'}})}\n\n"
                    yield f"data: {json.dumps({'done': True, 'session_id': session_id})}\n\n"
                    return
                
                yield f"data: {json.dumps({'text': {'content': 'Extracting success criteria...'}})}\n\n"
                
                criteria = await extract_goal_criteria(goal_text, side_query)
                if not criteria:
                    yield f"data: {json.dumps({'text': {'content': 'Could not extract verifiable success criteria. Try a more concrete goal.'}})}\n\n"
                    yield f"data: {json.dumps({'done': True, 'session_id': session_id})}\n\n"
                    return
                
                # Save pending goal to session
                if session_id:
                    session_data = load_session(session_id) or {}
                    session_data["pendingGoal"] = {"goal": goal_text, "criteria": criteria}
                    save_session(session_id, session_data)
                
                # Send criteria to frontend for confirmation
                yield f"data: {json.dumps({'goal_criteria': {'goal': goal_text, 'criteria': criteria}})}\n\n"
                yield f"data: {json.dumps({'done': True, 'session_id': session_id})}\n\n"
            
            return StreamingResponse(generate_extract_criteria(), media_type="text/event-stream")
        
        if full_message.startswith("/"):
            async def generate_command_response():
                cmd_result = ""
                cmd = full_message.split()[0].lower()
                
                if cmd == "/memory":
                    from agents.memory.memory import list_memories
                    memories = list_memories()
                    if not memories:
                        cmd_result = "No memories found."
                    else:
                        cmd_result = f"Found {len(memories)} memories:\n"
                        for m in memories[:20]:
                            cmd_result += f"- [{m.type}] {m.name}: {m.description}\n"
                
                elif cmd == "/skills":
                    from agents.skills.skills import discover_skills
                    skills = discover_skills()
                    if not skills:
                        cmd_result = "No skills found."
                    else:
                        cmd_result = f"Found {len(skills)} skills:\n"
                        for s in skills[:20]:
                            cmd_result += f"- {s.name}: {s.description}\n"
                
                elif cmd == "/compact":
                    try:
                        await svc.compact()
                        cmd_result = "Context compacted."
                    except Exception as e:
                        cmd_result = f"Failed to compact: {e}"
                
                elif cmd == "/clear":
                    svc.truncate_messages_to(1)
                    cmd_result = "Conversation cleared."
                
                elif cmd == "/cost":
                    stats = svc.get_stats()
                    cmd_result = f"Tokens: {stats.get('input', 0)} in / {stats.get('output', 0)} out"
                
                else:
                    cmd_result = f"Unknown command: {cmd}"
                
                yield f"data: {json.dumps({'text': {'content': cmd_result}})}\n\n"
                yield f"data: {json.dumps({'done': True, 'session_id': svc.session_id})}\n\n"
            
            return StreamingResponse(
                generate_command_response(),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",
                }
            )
        
        async def generate():
            _active_sessions = get_active_sessions()
            current_sub_agent_id = None
            
            session_id = svc.session_id or data.session_id or "unknown"
            _active_sessions[session_id] = {"svc": svc}
            
            try:
                async for event in svc.chat_stream(full_message):
                    event_type = event.get("type")
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
                    elif event_type == "stats":
                        stats_data = {
                            'stats': {
                                'input_tokens': event.get('input_tokens', 0),
                                'output_tokens': event.get('output_tokens', 0),
                                'context_window': event.get('context_window', 128000),
                                'last_input_token_count': event.get('last_input_token_count', 0),
                            }
                        }
                        yield f"data: {json.dumps(stats_data)}\n\n"
                    elif event_type == "sub_agent_start":
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
                    elif event_type == "done":
                        yield f"data: {json.dumps({'done': True, 'session_id': svc.session_id})}\n\n"
            finally:
                await svc.save()
                _active_sessions.pop(session_id, None)
                os.chdir(original_cwd)
        
        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            }
        )
    except Exception as e:
        import traceback
        error_msg = f"Error processing message: {str(e)}\n\n{traceback.format_exc()}"
        return JSONResponse(
            status_code=500,
            content={"error": error_msg, "session_id": data.session_id or "error-session"}
        )


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
