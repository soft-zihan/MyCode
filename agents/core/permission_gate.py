"""PermissionGate — 运行时权限交互。

职责：
- 危险命令确认弹窗（CLI / SSE / fallback）
- 路径缓存（同一会话内不重复询问）
- SSE 权限请求/响应管理

区别于：
- tools/permissions.py: 静态规则匹配（"这个命令危险吗？"）
- permission_set.py: Agent 级权限集（"这个 Agent 类型能编辑吗？"）
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any, Awaitable, Callable, Optional

from agents.logging import print_confirmation


class PermissionGate:
    def __init__(
        self,
        *,
        session: Any = None,
        confirm_fn: Optional[Callable[[str], Awaitable[bool]]] = None,
        sub_agent_id: Optional[str] = None,
        abort_fn: Optional[Callable[[], bool]] = None,
    ) -> None:
        self._session = session
        self._confirm_fn = confirm_fn
        self._sub_agent_id = sub_agent_id
        self._abort_fn = abort_fn

        self._confirmed_paths: set[str] = set()
        self._permission_responses: dict[str, dict[str, Any]] = {}
        self._current_tool_name: str = "unknown"

    @property
    def confirmed_paths(self) -> set[str]:
        return self._confirmed_paths

    def set_session(self, session: Any) -> None:
        self._session = session

    def set_confirm_fn(self, fn: Callable[[str], Awaitable[bool]]) -> None:
        self._confirm_fn = fn

    def set_sub_agent_id(self, sub_agent_id: str | None) -> None:
        self._sub_agent_id = sub_agent_id

    def set_abort_fn(self, fn: Callable[[], bool]) -> None:
        self._abort_fn = fn

    def set_current_tool_name(self, name: str) -> None:
        self._current_tool_name = name

    async def confirm(self, command: str, extra_data: dict | None = None) -> bool:
        print_confirmation(command)

        if self._confirm_fn:
            return await self._confirm_fn(command)

        if self._session is not None:
            return await self._confirm_via_session(command, extra_data=extra_data)

        try:
            answer = input("  Allow? (y/n): ")
            return answer.lower().startswith("y")
        except EOFError:
            return False

    async def _confirm_via_session(self, command: str, extra_data: dict | None = None) -> bool:
        request_id = str(uuid.uuid4())[:8]

        event_data = {
            "rpc_id": request_id,
            "request_id": request_id,
            "command": command,
            "tool_name": self._current_tool_name,
            "sub_agent_id": self._sub_agent_id,
        }
        if extra_data:
            event_data.update(extra_data)
        self._session.append("permission/request", event_data)

        for _ in range(3000):
            await asyncio.sleep(0.1)
            if request_id in self._permission_responses:
                response = self._permission_responses.pop(request_id)
                return response.get("allowed", False)
            if self._abort_fn and self._abort_fn():
                return False

        return False

    def set_response(self, request_id: str, allowed: bool) -> None:
        self._permission_responses[request_id] = {"allowed": allowed}

    def clear(self) -> None:
        self._confirmed_paths.clear()
        self._permission_responses.clear()
