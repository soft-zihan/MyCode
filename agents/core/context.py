"""Context Manager — 上下文管理逻辑封装。

职责：
- 消息增删
- 上下文压缩（compact）
- 折叠会话记忆
"""

from __future__ import annotations

from typing import Any, Callable
from agents.logging import print_info


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

    def append_tool_message(
        self,
        tool_call_id: str,
        content: str,
        tool_name: str = "",
        outcome: str = "",
    ) -> None:
        """追加工具结果消息到事件日志。

        outcome / tool_name 一并落盘：验收闸门需要事后回答「这条任务在做的时候
        有没有跑过成功的 shell 命令」，而工具名此前只能从 <tool_result tool="...">
        的包裹文本里解析，脆弱。取消/中止路径不传这两个参数，落空串。

        outcome 逐字存，不归一化：词表来自 agents/tools/result.py 的 ToolOutcome
        （success/error/timeout/cancelled/blocked/budget_exceeded），闸门按
        == "success" 精确比较，这里任何"顺手规整"都会让事实源失去可追溯性。
        """
        wrapped = f"<tool_result tool=\"{tool_name}\">\n{content}\n</tool_result>"
        self.agent.session.append("tool_result_msg", {
            "call_id": tool_call_id,
            "content": wrapped,
            "tool_name": tool_name,
            "outcome": outcome,
        })

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
        self.agent.reset_context_token_estimate()
        print_info("Conversation cleared.")

    def clear_history_keep_system(self) -> None:
        """清空历史信息，保留系统 prompt。"""
        self.agent.session._log.clear()
        self.agent.session.system_prompt = self.agent._system_prompt
        self.agent.reset_context_token_estimate()
        self.agent._fold_last_time = 0.0
        self.agent._fold_count = 0
        self.agent._tool_error_streak = 0
        self.agent._same_tool_repeat_count = 0
        self.agent._last_tool_name = ""

    async def compact(self) -> bool:
        """手动压缩会话。返回是否实际执行了折叠。"""
        compacted = await self._compact_conversation(trigger="manual")
        if not compacted:
            print_info("Nothing to compact yet.")
        return compacted

    async def _check_and_compact(
        self, defer_fold: Callable[[], bool] | bool | None = None) -> None:
        """自动检查并压缩。

        defer_fold: 折叠任务边界门（Plan 2 Task 4）的输入。生产路径上是
        Agent._has_in_progress_task **这个零参 callable 本身**（不是它的返回值）：
        探针要读一次任务清单，而那次读只在压缩器真的逼近触发点时才值得发生，所以
        解析权在 ContextCompressor._resolve_defer_fold。

        这一跳是纯中转：不在这里调用探针、不在这里探测任务清单（失败方向的裁定权
        属于 Agent），也不给参数加任何语义。
        """
        folded = await self.agent._compressor.run_pipeline(
            self.agent.session,
            self.agent.estimated_context_tokens,
            self.agent.last_api_call_time,
            self.agent._build_side_query(max_tokens=6000),
            self.agent.session_id,
            defer_fold=defer_fold,
        )
        if folded:
            self.agent.reset_context_token_estimate()

    async def _compact_conversation(self, *, trigger: str = "manual") -> bool:
        """压缩会话。"""
        compacted = await self.agent._compressor.compact_manual(
            self.agent.session,
            self.agent._build_side_query(max_tokens=6000),
            self.agent.session_id,
        )
        if compacted:
            print_info("Conversation compacted.")
            self.agent.reset_context_token_estimate()
        return compacted

    async def _compact_openai(self, *, trigger: str) -> bool:
        """使用 OpenAI 压缩。"""
        return await self._compact_conversation(trigger=trigger)

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
