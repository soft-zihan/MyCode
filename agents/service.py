"""AgentService — Agent 业务操作门面。

CLI 和 Web 统一通过此类与 Agent 交互，消除对 Agent 私有属性的直接访问。
"""

from __future__ import annotations

from typing import Any, AsyncIterator


class AgentService:
    def __init__(self, agent: Any) -> None:
        self._agent = agent

    # ── 对话 ──

    async def chat(self, message: str) -> None:
        await self._agent.chat(message)

    async def chat_stream(self, message: str) -> AsyncIterator[dict]:
        async for event in self._agent.chat_stream(message):
            yield event

    async def run_once(self, message: str) -> None:
        await self._agent.chat(message)
        await self._agent.drain_background_skill_tasks()

    # ── 控制 ──

    def abort(self) -> None:
        self._agent.abort()

    def steer(self, message: str) -> None:
        self._agent.steer(message)

    def set_permission_mode(self, mode: str) -> None:
        self._agent.set_permission_mode(mode)

    def respond_permission(self, request_id: str, allowed: bool) -> None:
        self._agent.set_permission_response(request_id, allowed)

    def set_confirm_fn(self, fn) -> None:
        self._agent.set_confirm_fn(fn)

    # ── Session 持久化 ──

    async def save(self) -> None:
        await self._agent.save()

    def restore(self, session_data: dict) -> None:
        self._agent.restore_session(session_data)

    def rewind(self, n: int = 1) -> str:
        return self._agent.rewind(n)

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

    @property
    def agent(self) -> Any:
        return self._agent
