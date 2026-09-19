"""Session Manager — 会话管理逻辑封装。

职责：
- 会话恢复（restore）
- 会话回退（rewind）
- 会话分支（fork）
- 三阶段恢复（stage/clear/commit revert）
"""

from __future__ import annotations

from typing import Any


class SessionManager:
    """会话管理器。"""

    def __init__(self, *, agent_ref: Any):
        """
        Args:
            agent_ref: Agent 实例引用
        """
        self.agent = agent_ref

    def restore_session(self, data: dict) -> None:
        """恢复会话信息。"""
        from agents.core.session_lifecycle import SessionState
        from agents.logging import print_info

        state = SessionState(
            session_id=self.agent.session_id,
            model=self.agent.model,
        )
        self.agent._session_lifecycle.restore(state, data, self.agent.session)
        print_info(f"Session restored ({self._get_message_count()} messages).")

    def rewind(self, n: int = 1) -> str:
        """回退对话 N 轮。"""
        return self.agent._session_lifecycle.rewind(self.agent.session, n)

    def stage_revert(self, target_seq: int) -> dict:
        """Stage：计算恢复计划，预览变更。"""
        return self.agent.session.stage_revert(target_seq)

    def clear_revert(self, current_snapshot: list[dict]) -> dict:
        """Clear：取消恢复，恢复到原始状态。"""
        return self.agent.session.clear_revert(current_snapshot)

    def commit_revert(self, target_seq: int) -> dict:
        """Commit：确认恢复。"""
        return self.agent.session.commit_revert(target_seq)

    def fork_session(self) -> str:
        """从当前会话创建一个完全相同的分支。"""
        from agents.core.session_lifecycle import SessionState

        state = SessionState(
            session_id=self.agent.session_id,
            model=self.agent.model,
            start_time=self.agent.session_start_time,
            cwd=str(self.agent.workspace),
        )
        result, new_session = self.agent._session_lifecycle.fork(state, self.agent.session)
        self.agent.session = new_session
        self.agent.session_id = new_session.id
        return result

    def _get_message_count(self) -> int:
        """获取消息数量。"""
        return len(self.agent.session.get_messages_for_llm())
