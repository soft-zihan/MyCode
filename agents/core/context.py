"""Context Manager — 上下文管理逻辑封装。

职责：
- 消息增删
- 上下文压缩（compact）
- 折叠会话记忆
"""

from __future__ import annotations

import time
from typing import Any


class ContextManager:
    """上下文管理器。"""

    def __init__(self, *, agent_ref: Any):
        """
        Args:
            agent_ref: Agent 实例引用
        """
        self.agent = agent_ref

    def get_messages(self) -> list[dict]:
        """获取 LLM 消息历史（从事件日志派生）。"""
        return self.agent.session.get_messages_for_llm()

    def truncate_messages_to(self, index: int) -> None:
        """截断事件日志到指定索引。"""
        messages = self.agent.session.get_messages_for_llm()
        if index >= len(messages):
            return
        msg_count = 0
        for i, event in enumerate(self.agent.session._log):
            if event["type"] in ("user_message", "assistant_message", "tool_result_msg", "memory_injection"):
                if msg_count >= index:
                    self.agent.session._log = self.agent.session._log[:i]
                    return
                msg_count += 1

    def append_user_message(self, content: str, snapshot_id: str | None = None) -> None:
        """追加用户消息到事件日志。"""
        data = {"content": content}
        if snapshot_id:
            data["snapshot_id"] = snapshot_id
        self.agent.session.append("user_message", data)
        self.agent._user_message_written_this_turn = True

    def append_memory_injection(self, content: str) -> None:
        """追加记忆/Wiki 注入到事件日志。"""
        self.agent.session.append("memory_injection", {"content": content})

    def append_tool_message(self, tool_call_id: str, content: str, tool_name: str = "") -> None:
        """追加工具结果消息到事件日志。"""
        wrapped = f"<tool_result tool=\"{tool_name}\">\n{content}\n</tool_result>"
        self.agent.session.append("tool_result_msg", {"call_id": tool_call_id, "content": wrapped})

    def clear_history(self) -> None:
        """清空历史记录。"""
        self.agent.session._log.clear()
        self.agent.session.system_prompt = self.agent._system_prompt
        self.agent._skill_orchestrator.reset()
        self.agent._fold_last_time = 0.0
        self.agent._fold_count = 0
        self.agent._tool_error_streak = 0
        self.agent._same_tool_repeat_count = 0
        self.agent._last_tool_name = ""
        self.agent.total_input_tokens = 0
        self.agent.total_output_tokens = 0
        self.agent.last_input_token_count = 0
        from agents.logging import print_info
        print_info("Conversation cleared.")

    def clear_history_keep_system(self) -> None:
        """清空历史信息，保留系统 prompt。"""
        self.agent.session._log.clear()
        self.agent.session.system_prompt = self.agent._system_prompt
        self.agent.last_input_token_count = 0
        self.agent._fold_last_time = 0.0
        self.agent._fold_count = 0
        self.agent._tool_error_streak = 0
        self.agent._same_tool_repeat_count = 0
        self.agent._last_tool_name = ""

    async def compact(self) -> None:
        """手动压缩会话。"""
        compacted = await self._compact_conversation(trigger="manual")
        if not compacted:
            from agents.logging import print_info
            print_info("Nothing to compact yet.")

    async def _check_and_compact(self) -> None:
        """自动检查并压缩。"""
        folded = await self.agent._compressor.run_pipeline(
            self.agent.session,
            self.agent.last_input_token_count,
            self.agent.last_api_call_time,
            self.agent._build_side_query(max_tokens=6000),
            self.agent.session_id,
            self.agent._folded_session_memories,
        )
        if folded:
            self.agent.last_input_token_count = 0

    async def _compact_conversation(self, *, trigger: str = "manual") -> bool:
        """压缩会话。"""
        compacted = await self.agent._compressor.compact_manual(
            self.agent.session,
            self.agent._build_side_query(max_tokens=6000),
            self.agent.session_id,
            self.agent._folded_session_memories,
        )
        if compacted:
            from agents.observability.trace import trace_event
            trace_event(
                "compact",
                trigger=trigger,
                messages_after=self._get_message_count(),
                ctx_tokens=self.agent.last_input_token_count,
            )
            from agents.logging import print_info
            print_info("Conversation compacted.")
            self.agent.last_input_token_count = 0
        return compacted

    async def _compact_openai(self, *, trigger: str) -> bool:
        """使用 OpenAI 压缩。"""
        return await self._compact_conversation(trigger=trigger)

    async def _generate_folded_session_memory(self, transcript: str) -> dict[str, Any]:
        """生成折叠会话记忆。"""
        side_query = self.agent._build_side_query(max_tokens=6000)
        from agents.core.session_memory import fallback_folded_memory
        return fallback_folded_memory(transcript)

    async def _record_folded_session_memory(self, trigger: str, memory: dict[str, Any]) -> None:
        """记录折叠会话记忆。"""
        record = {
            "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "trigger": trigger,
            "session_id": self.agent.session_id,
            **memory,
        }
        self.agent._folded_session_memories.append(record)

        if not self.agent.is_sub_agent:
            try:
                from agents.wiki.wiki_capture import capture_session_to_session
                capture_session_to_session(self.agent.session_id, memory)
            except Exception:
                pass

    def _get_message_count(self) -> int:
        """获取消息数量。"""
        return len(self.agent.session.get_messages_for_llm())

    def describe_context(self) -> list[dict]:
        """描述上下文（供 /context 命令使用）。"""
        return self.agent._session_lifecycle.describe(self.agent.session.get_messages_for_llm())

    def delete_context_messages(self, indexes: list[int]) -> str:
        """删除指定索引的消息组。"""
        return self.agent._session_lifecycle.delete_messages(self.agent.session, indexes)

    def keep_context_messages(self, indexes: list[int]) -> str:
        """只保留指定索引的消息组。"""
        return self.agent._session_lifecycle.keep_messages(self.agent.session, indexes)
