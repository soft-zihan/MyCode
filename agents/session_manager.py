"""SessionManager.

SessionManager: 管理多 session 生命周期，创建/恢复/删除 session。
"""

from __future__ import annotations

import asyncio
from typing import Any

from agents.core.session import Session, session_dir


class SessionManager:
    """管理多 session 生命周期。"""
    
    def __init__(self):
        self._sessions: dict[str, Session] = {}
        self._agents: dict[str, Any] = {}
        self._chat_tasks: dict[str, asyncio.Task] = {}
    
    def create(
        self,
        model: str | None = None,
        agent_type: str | None = None,
        permission_mode: str | None = None,
        cwd: str | None = None,
    ) -> tuple[Any, Session]:
        """创建新 session 和 agent。"""
        from agents.agent import Agent
        from agents.config import load_config
        import logging
        logger = logging.getLogger(__name__)
        
        # Abort all existing agents and cancel their chat tasks before creating new session
        for session_id, old_agent in list(self._agents.items()):
            try:
                print(f"[DEBUG] session_manager.create: Aborting old agent {session_id}")
                old_agent.abort()
            except Exception as e:
                logger.warning(f"Failed to abort old agent {session_id}: {e}")
        
        # Cancel all existing chat tasks
        for session_id, task in list(self._chat_tasks.items()):
            if not task.done():
                print(f"[DEBUG] session_manager.create: Cancelling old chat task {session_id}")
                task.cancel()
        self._chat_tasks.clear()
        
        config = load_config()
        api_key, api_base, model_name = self._resolve_model(config, model)
        
        session = Session()
        print(f"[DEBUG] session_manager.create: session.id = {session.id}")
        if cwd:
            # 工作区走事件流（单一数据源）：session/created → cwd 投影 → 快照/列表/重建
            session.append("session/created", {"cwd": cwd})
        agent = Agent(
            model=model_name,
            api_key=api_key,
            api_base=api_base,
            permission_mode=permission_mode or "bypassPermissions",
            session_id=session.id,
            workspace=cwd,
        )
        print(f"[DEBUG] session_manager.create: agent.session_id = {agent.session_id}, agent.session.id = {agent.session.id}")
        agent.session = session
        agent.session_id = session.id  # 确保 session_id 一致
        print(f"[DEBUG] session_manager.create: AFTER assignment - agent.session_id = {agent.session_id}, agent.session.id = {agent.session.id}, id(agent.session) = {id(agent.session)}")
        
        print(f"[DEBUG] session_manager.create: RETURNING session.id = {session.id}")
        self._sessions[session.id] = session
        self._agents[session.id] = agent
        
        return agent, session
    
    def register_chat_task(self, session_id: str, task: asyncio.Task) -> None:
        """Register a chat task for a session so it can be cancelled later."""
        self._chat_tasks[session_id] = task
    
    def restore(self, session_id: str, cwd: str | None = None) -> tuple[Any, Session] | None:
        """恢复 session。优先从内存获取，其次从 JSONL，最后从旧 JSON 文件。"""
        import logging
        logger = logging.getLogger(__name__)
        
        logger.info(f"[DEBUG] Attempting to restore session: {session_id}")
        
        # 1. 先检查内存
        existing_agent = self.get_agent(session_id)
        existing_session = self.get(session_id)
        if existing_agent and existing_session:
            logger.info(f"[DEBUG] Session {session_id} found in memory")
            return existing_agent, existing_session
        
        # 2. 尝试从 JSONL 加载
        session = Session.load_from_events(session_id)
        
        # 3. 如果 JSONL 不存在，尝试从旧 JSON 文件恢复
        if not session:
            session = self._restore_from_json(session_id)
        if not session:
            return None
        
        from agents.agent import Agent
        from agents.config import load_config
        
        config = load_config()
        api_key, api_base, model_name = self._resolve_model(config, None)
        
        agent = Agent(
            model=model_name,
            api_key=api_key,
            api_base=api_base,
            session_id=session.id,
            workspace=cwd or session.projections.get("cwd"),
        )
        agent.session = session
        
        agent._current_turn = max(
            (e.get("turn", 0) for e in session._log if e["type"] == "turn/start"),
            default=0,
        )
        
        # 重建 agent 的消息历史
        self._rebuild_agent_messages(agent, session)
        
        self._sessions[session_id] = session
        self._agents[session_id] = agent
        
        return agent, session
    
    def _restore_from_json(self, session_id: str) -> Session | None:
        """从旧 JSON session 文件恢复，转换为事件日志格式。"""
        from agents.core.session import load_session as load_json_session
        
        data = load_json_session(session_id)
        if not data:
            return None
        
        session = Session(session_id)
        
        openai_messages = data.get("openaiMessages", [])
        for msg in openai_messages:
            role = msg.get("role", "")
            content = msg.get("content", "")
            
            if role == "user":
                if isinstance(content, str):
                    session.append("user_message", {"content": content})
            elif role == "assistant":
                thinking = msg.get("thinking")
                tool_calls = msg.get("tool_calls")
                session.append("assistant_message", {
                    "content": content or "",
                    "thinking": thinking,
                    "tool_calls": tool_calls,
                })
            elif role == "tool":
                session.append("tool_result_msg", {
                    "call_id": msg.get("tool_call_id", ""),
                    "content": content or "",
                })
        
        return session if session._log else None
    
    def _rebuild_agent_messages(self, agent: Any, session: Session) -> None:
        """从 session 事件日志重建 agent 的系统提示词。"""
        # 系统提示词存储在 session.system_prompt 中
        session.system_prompt = agent._system_prompt
    
    def get(self, session_id: str) -> Session | None:
        return self._sessions.get(session_id)
    
    def get_agent(self, session_id: str) -> Any | None:
        return self._agents.get(session_id)
    
    def active_sessions(self) -> list[Session]:
        return list(self._sessions.values())
    
    def remove(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)
        self._agents.pop(session_id, None)
    
    def abort(self, session_id: str) -> None:
        """Abort a session's agent and cancel its chat task."""
        if agent := self._agents.get(session_id):
            try:
                agent.abort()
            except Exception:
                pass
        if task := self._chat_tasks.get(session_id):
            if not task.done():
                task.cancel()
    
    def _resolve_model(self, config: Any, model: str | None) -> tuple[str | None, str | None, str]:
        if model:
            for ep in config.endpoints.values():
                if ep.model == model:
                    return ep.api_key, ep.base_url, ep.model
        if config.endpoints:
            first = next(iter(config.endpoints.values()))
            return first.api_key, first.base_url, first.model
        return None, None, model or "unknown"


_session_manager: SessionManager | None = None


def get_session_manager() -> SessionManager:
    global _session_manager
    if _session_manager is None:
        _session_manager = SessionManager()
    return _session_manager
