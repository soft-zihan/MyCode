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
        auto_approve_fn: Optional[Callable[[], bool]] = None,
    ) -> None:
        self._session = session
        self._confirm_fn = confirm_fn
        self._sub_agent_id = sub_agent_id
        self._abort_fn = abort_fn
        # 会话级自动审批探针。**刻意是零参 callable 而不是 bool 快照**：
        # _confirm_via_session 每 0.1s 轮询都重新读它，于是「等待期间翻转开关」
        # 天然即时生效（热生效），不需要任何唤醒机制。折叠任务边界门是同一个
        # 模式（agent.check_and_compact 交给压缩器的也是零参 callable）。
        self._auto_approve_fn = auto_approve_fn

        self._confirmed_paths: set[str] = set()
        self._permission_responses: dict[str, dict[str, Any]] = {}
        self._current_tool_name: str = "unknown"
        self.last_feedback: str = ""
        self.last_choice: str = ""

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

    def set_auto_approve_fn(self, fn: Callable[[], bool]) -> None:
        self._auto_approve_fn = fn

    def set_current_tool_name(self, name: str) -> None:
        self._current_tool_name = name

    async def confirm(self, command: str, extra_data: dict | None = None) -> bool:
        self.last_feedback = ""
        self.last_choice = ""

        # 自动审批开关已开 → 直接放行，**一个事件都不落**（裁定 2）。
        # 「不落事件」的边界：仅指入口处开关已开、不凭空造请求。若请求已落盘、
        # 开关是在等待期间才打开的，_confirm_via_session **必须**补一条
        # permission/resolved——否则历史里是「有 request、无 resolved」，前端的
        # 待审批恢复会把这个早已放行的请求复活成幽灵审批对话框。
        #
        # 语义注意：自动放行时 last_choice 保持空串，plan 审批层取
        # `agent._permission_gate.last_choice or "execute"`（plan_tool_executor），
        # 于是自动审批等于点「批准并自动执行」，**不是**「逐步确认」。
        if self._auto_approve_fn and self._auto_approve_fn():
            return True

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

        # 无界等待（缺陷 A 修复）：原来 300 秒耗尽后 return False，plan 审批层把
        # False 翻译成「用户拒绝了计划且无意见」，模型据此静默重写一个没人看过的
        # 计划。现在未应答的请求就一直等，出口只有三条，且**每条都追加
        # permission/resolved**——前端从历史恢复待审批靠 request/resolved 配对判定
        # 「是否仍未决议」，此路径原先一条 resolved 都不发：
        #   ① 人工应答（outcome = allowed / denied）
        #   ② abort（outcome = aborted）。_abort_fn 绑的是 agent._abort_requested
        #      （agent._confirm_dangerous 里 set_abort_fn），这是无界循环唯一的
        #      非人工出口，测试钉住了它真能把循环放出来。
        #   ③ 等待期间开关被打开（outcome = auto）——裁定 2 的边界，见 confirm()。
        while True:
            await asyncio.sleep(0.1)
            if request_id in self._permission_responses:
                response = self._permission_responses.pop(request_id)
                allowed = bool(response.get("allowed", False))
                self.last_feedback = response.get("feedback", "") or ""
                self.last_choice = response.get("choice", "") or ""
                self._session.append("permission/resolved", {
                    "rpc_id": request_id,
                    "outcome": "allowed" if allowed else "denied",
                })
                return allowed
            if self._abort_fn and self._abort_fn():
                self._session.append("permission/resolved", {
                    "rpc_id": request_id,
                    "outcome": "aborted",
                })
                return False
            if self._auto_approve_fn and self._auto_approve_fn():
                self._session.append("permission/resolved", {
                    "rpc_id": request_id,
                    "outcome": "auto",
                })
                return True

    def set_response(self, request_id: str, allowed: bool, feedback: str = "", choice: str = "") -> None:
        self._permission_responses[request_id] = {"allowed": allowed, "feedback": feedback, "choice": choice}

    def clear(self) -> None:
        self._confirmed_paths.clear()
        self._permission_responses.clear()
