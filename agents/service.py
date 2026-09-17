"""AgentService — Agent 业务操作门面。

CLI 和 Web 统一通过此类与 Agent 交互，消除对 Agent 私有属性的直接访问。
提供并发控制、健康检查、优雅关闭等生产级能力。
"""

from __future__ import annotations

import asyncio
import os
import signal
import time
from typing import Any, AsyncIterator

from agents.core.request_context import new_request_id


class AgentService:
    _session_locks: dict[str, asyncio.Lock] = {}
    _global_semaphore: asyncio.Semaphore | None = None
    _start_time = time.time()
    _shutting_down = False

    def __init__(self, agent: Any) -> None:
        self._agent = agent
        if AgentService._global_semaphore is None:
            max_concurrent = int(os.environ.get("MYCODE_MAX_CONCURRENT", "3"))
            AgentService._global_semaphore = asyncio.Semaphore(max_concurrent)

    # ── 并发控制 ──

    async def _acquire_session_lock(self) -> None:
        session_id = self._agent.session_id
        if session_id not in self._session_locks:
            self._session_locks[session_id] = asyncio.Lock()
        await self._session_locks[session_id].acquire()

    def _release_session_lock(self) -> None:
        session_id = self._agent.session_id
        if session_id in self._session_locks:
            lock = self._session_locks[session_id]
            if lock.locked():
                lock.release()

    async def _acquire_concurrency_slot(self) -> None:
        assert self._global_semaphore is not None
        await self._global_semaphore.acquire()

    def _release_concurrency_slot(self) -> None:
        assert self._global_semaphore is not None
        self._global_semaphore.release()

    # ── 对话 ──

    async def chat(self, message: str) -> None:
        new_request_id()
        await self._acquire_session_lock()
        await self._acquire_concurrency_slot()
        try:
            await self._agent.chat(message)
        finally:
            self._release_concurrency_slot()
            self._release_session_lock()

    async def chat_stream(self, message: str) -> AsyncIterator[dict]:
        new_request_id()
        await self._acquire_session_lock()
        await self._acquire_concurrency_slot()
        try:
            async for event in self._agent.chat_stream(message):
                yield event
        finally:
            self._release_concurrency_slot()
            self._release_session_lock()

    async def run_once(self, message: str) -> None:
        new_request_id()
        await self._acquire_session_lock()
        await self._acquire_concurrency_slot()
        try:
            await self._agent.chat(message)
            await self._agent.drain_background_skill_tasks()
        finally:
            self._release_concurrency_slot()
            self._release_session_lock()

    # ── 控制 ──

    def abort(self) -> None:
        self._agent.abort()

    def steer(self, message: str) -> None:
        self._agent.steer(message)

    def set_permission_mode(self, mode: str) -> None:
        self._agent.set_permission_mode(mode)

    def respond_permission(self, request_id: str, allowed: bool) -> None:
        self._agent.set_permission_response(request_id, allowed)

    def respond_question(self, request_id: str, answer: str) -> None:
        self._agent.session.question_responses[request_id] = {"answer": answer}

    def set_confirm_fn(self, fn) -> None:
        self._agent.set_confirm_fn(fn)

    # ── Session 持久化 ──

    async def save(self) -> None:
        await self._agent.save()

    def restore(self, session_data: dict) -> None:
        self._agent.restore_session(session_data)

    def rewind(self, n: int = 1) -> str:
        return self._agent.rewind(n)

    # ── 三阶段恢复 ──
    
    def stage_revert(self, target_seq: int) -> dict:
        """Stage：计算恢复计划，预览变更。"""
        return self._agent.stage_revert(target_seq)
    
    def clear_revert(self, current_snapshot: list[dict]) -> dict:
        """Clear：取消恢复，恢复到原始状态。"""
        return self._agent.clear_revert(current_snapshot)
    
    def commit_revert(self, target_seq: int) -> dict:
        """Commit：确认恢复。"""
        return self._agent.commit_revert(target_seq)

    def fork(self) -> str:
        return self._agent.fork_session()

    async def compact(self) -> None:
        await self._agent.compact()

    # ── 上下文操作 ──

    def describe_context(self) -> list[dict]:
        return self._agent.describe_context()

    def delete_messages(self, indexes: list[int]) -> str:
        return self._agent.delete_context_messages(indexes)

    def keep_messages(self, indexes: list[int]) -> str:
        return self._agent.keep_context_messages(indexes)

    def truncate_messages_to(self, index: int) -> None:
        self._agent.truncate_messages_to(index)

    # ── 查询 ──

    def get_messages(self) -> list[dict]:
        return self._agent.get_messages()

    def get_stats(self) -> dict:
        return self._agent.get_token_usage()

    @property
    def last_response(self) -> str:
        return self._agent.last_response

    @property
    def session_id(self) -> str:
        return self._agent.session_id

    def set_session_id(self, session_id: str) -> None:
        self._agent.session_id = session_id

    @property
    def agent(self) -> Any:
        return self._agent

    # ── 健康检查 ──

    def health_check(self) -> dict:
        return {
            "status": "ok",
            "uptime_seconds": int(time.time() - self._start_time),
            "session_id": self._agent.session_id,
            "active_sessions": len(self._session_locks),
            "shutting_down": self._shutting_down,
        }

    # ── 优雅关闭 ──

    def setup_graceful_shutdown(self) -> None:
        signal.signal(signal.SIGTERM, self._handle_shutdown_signal)
        signal.signal(signal.SIGINT, self._handle_shutdown_signal)

    def _handle_shutdown_signal(self, signum: int, frame: Any) -> None:
        if self._shutting_down:
            return
        self._shutting_down = True
        asyncio.create_task(self._graceful_shutdown())

    async def _graceful_shutdown(self) -> None:
        self._agent.abort()
        await self._agent.drain_background_skill_tasks()
        await self._agent.save()
        import sys
        sys.exit(0)
