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
        self._restore_locks: dict[str, asyncio.Lock] = {}  # 每个 session 的 restore 锁
    
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
        
        config = load_config()
        api_key, api_base, model_name = self._resolve_model(config, model)
        
        # agent_type="plan" 时强制使用 plan permission mode
        if agent_type == "plan":
            permission_mode = "plan"
        
        session = Session()
        print(f"[DEBUG] session_manager.create: session.id = {session.id}, agent_type={agent_type}, permission_mode={permission_mode}")
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
        session.system_prompt = agent._system_prompt  # 确保 session 的系统提示词被设置
        print(f"[DEBUG] session_manager.create: AFTER assignment - agent.session_id = {agent.session_id}, agent.session.id = {agent.session.id}, id(agent.session) = {id(agent.session)}")
        
        print(f"[DEBUG] session_manager.create: RETURNING session.id = {session.id}")
        self._sessions[session.id] = session
        self._agents[session.id] = agent
        
        return agent, session
    
    def register_chat_task(self, session_id: str, task: asyncio.Task) -> None:
        """Register a chat task for a session so it can be cancelled later."""
        self._chat_tasks[session_id] = task
    
    async def restore(self, session_id: str, cwd: str | None = None) -> tuple[Any, Session] | None:
        """恢复 session。优先从内存获取，其次从 JSONL，最后从旧 JSON 文件。"""
        import logging
        logger = logging.getLogger(__name__)
        
        logger.info(f"[DEBUG] Attempting to restore session: {session_id}")
        
        # 获取 session 级别的 restore 锁，防止并发 restore
        if session_id not in self._restore_locks:
            self._restore_locks[session_id] = asyncio.Lock()
        async with self._restore_locks[session_id]:
            # 1. 先检查内存（在锁内再次检查，防止并发创建）
            existing_agent = self.get_agent(session_id)
            existing_session = self.get(session_id)
            if existing_agent and existing_session:
                logger.info(f"[DEBUG] Session {session_id} found in memory")
                return existing_agent, existing_session
            
            # 2. 尝试从 JSONL 加载
            session = Session.load_from_events(session_id)
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
