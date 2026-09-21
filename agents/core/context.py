"""Context Manager — 上下文管理逻辑封装。

职责：
- 消息增删
- 上下文压缩（compact）
- 折叠会话记忆
"""

from __future__ import annotations

import asyncio
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
        self.agent.reset_context_token_estimate()
        from agents.logging import print_info
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
            self.agent.estimated_context_tokens,
            self.agent.last_api_call_time,
            self.agent._build_side_query(max_tokens=6000),
            self.agent.session_id,
            self.agent._folded_session_memories,
        )
        if folded:
            self.agent.reset_context_token_estimate()

    async def _compact_conversation(self, *, trigger: str = "manual") -> bool:
        """压缩会话。"""
        compacted = await self.agent._compressor.compact_manual(
            self.agent.session,
            self.agent._build_side_query(max_tokens=6000),
            self.agent.session_id,
            self.agent._folded_session_memories,
        )
        if compacted:
            from agents.logging import print_info
            print_info("Conversation compacted.")
            self.agent.reset_context_token_estimate()
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
            "time": time.strftime("%Y-%m-%dT%H:%MZ", time.gmtime()),
            "trigger": trigger,
            "session_id": self.agent.session_id,
            **memory,
        }
        self.agent._folded_session_memories.append(record)

        if not self.agent.is_sub_agent:
            try:
                from agents.wiki.wiki_capture import (
                    capture_session_to_session, 
                    get_last_extract_pos,
                )
                
                # 获取所有事件
                all_events = self.agent.session._log
                
                # 获取上次提取位置（水位线）
                last_pos = get_last_extract_pos(self.agent.session_id)
                
                # 只提取新事件（自上次水位线以来）
                new_events = [e for e in all_events if e.get("seq", 0) > last_pos]
                
                if new_events:
                    # 计算分段序号（基于已存在的文件数量）
                    from agents.wiki.wiki_manager import get_wiki_dir
                    from datetime import datetime, timezone
                    now = datetime.now(timezone.utc)
                    session_dir = get_wiki_dir() / "session" / now.strftime("%Y/%m/%d")
                    slug = self.agent.session_id.replace("/", "_").replace("\\", "_")[:40]
                    existing_segments = list(session_dir.glob(f"{slug}_seg*.md")) if session_dir.exists() else []
                    segment_index = len(existing_segments) + 1
                    
                    # 捕获新事件到 session 文件
                    # 注意：水位线不在此推进，编译成功后由 _compile_single_session_async 更新；
                    # 失败时 segment 保持 compiled=false，由补编译机制重试
                    captured_path = capture_session_to_session(self.agent.session_id, new_events, segment_index)

                    # 立即编译当前段，而不是攒文件
                    if captured_path:
                        side_query = self.agent._build_side_query(max_tokens=6000)
                        asyncio.create_task(self._compile_single_session_async(captured_path, side_query))
            except Exception as e:
                print(f"[wiki_capture] fold capture failed for {self.agent.session_id}: {type(e).__name__}: {e}")

    async def _compile_single_session_async(self, session_path: Any, side_query: Any) -> None:
        """异步编译单个 session 文件。编译成功后才推进水位线。"""
        try:
            from agents.wiki.wiki_compiler import compile_single_session, check_and_compile_pending_sessions
            result = await compile_single_session(session_path, side_query)
            if result is None:
                # compile lock 被占用，segment 保持 compiled=false，由补编译机制重试
                print(f"[wiki_compile_single] lock busy, deferred: {session_path.name}")
                return
            
            # 编译成功：从 frontmatter 读取 max_seq，推进水位线
            try:
                from agents.core.frontmatter import parse_frontmatter
                from agents.wiki.wiki_capture import get_last_extract_pos, set_last_extract_pos
                meta = parse_frontmatter(session_path.read_text()).meta
                session_id = meta.get("session_id", "")
                seg_max_seq = int(meta.get("max_seq", "0"))
                if session_id and seg_max_seq > get_last_extract_pos(session_id):
                    set_last_extract_pos(session_id, seg_max_seq)
            except Exception as e:
                print(f"[wiki_compile_single] watermark update failed for {session_path.name}: {type(e).__name__}: {e}")
                raise
            
            total = sum(v for k, v in result.items() if isinstance(v, int))
            if total > 0:
                print(f"[wiki_compile] extracted {total} entries from single session")
            
            # 编译完成后，检查其他 session 是否需要补编译
            backfill_result = await check_and_compile_pending_sessions(side_query, threshold=20)
            backfill_total = sum(v for k, v in backfill_result.items() if isinstance(v, int))
            if backfill_total > 0:
                print(f"[wiki_backfill] extracted {backfill_total} entries from pending sessions")
        except Exception as e:
            # 编译失败：水位线不推进，segment 保持 compiled=false，下次补编译自动重试
            print(f"[wiki_compile_single] compile failed for {session_path.name}: {type(e).__name__}: {e}")

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
