"""ContextCompressor — 两级上下文压缩管线。

职责：
- 策略 A：工具调用折叠（上下文 > 60% 且工具占比 > 70%，或空闲 > 5 分钟）
- 策略 B：会话折叠（上下文 > 60% 且工具占比 < 70%，或手动触发）

压缩状态通过标记事件持久化到事件日志（append-only）：
- context/tool_snipped: 记录被替换的工具结果 seq 和 key
- context/events_deleted: 记录被删除的 seq 列表
- context/session_folded: 写入折叠摘要
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Callable, Awaitable

from .context_store import (
    ContextStore,
    is_compressed_placeholder,
    snipped_placeholder,
)
from .session_memory import (
    build_openai_transcript,
    fallback_folded_memory,
    format_folded_memory,
    FOLD_SESSION_MEMORY_SYSTEM,
)
from .session import save_folded_session_memory
from agents.logging import print_info

TOOL_FOLD_THRESHOLD = 0.60
SESSION_FOLD_THRESHOLD = 0.60
TOOL_RATIO_THRESHOLD = 0.70
KEEP_RECENT_TOOL_ROUNDS = 3
KEEP_RECENT_DIALOG_ROUNDS = 2
IDLE_TIMEOUT_S = 5 * 60
SINGLE_RESULT_CHAR_LIMIT = 30000

SideQueryFn = Callable[[str, str], Awaitable[str]]


def _sanitize_for_utf8(value: Any) -> Any:
    if isinstance(value, str):
        return value.encode("utf-8", errors="replace").decode("utf-8")
    if isinstance(value, list):
        return [_sanitize_for_utf8(v) for v in value]
    if isinstance(value, dict):
        return {k: _sanitize_for_utf8(v) for k, v in value.items()}
    return value


class ContextCompressor:
    def __init__(
        self,
        context_store: ContextStore,
        *,
        tool_fold_threshold: float = TOOL_FOLD_THRESHOLD,
        session_fold_threshold: float = SESSION_FOLD_THRESHOLD,
        tool_ratio_threshold: float = TOOL_RATIO_THRESHOLD,
        keep_recent_tool_rounds: int = KEEP_RECENT_TOOL_ROUNDS,
        keep_recent_dialog_rounds: int = KEEP_RECENT_DIALOG_ROUNDS,
        idle_timeout_seconds: int = IDLE_TIMEOUT_S,
        single_result_char_limit: int = SINGLE_RESULT_CHAR_LIMIT,
        effective_window: int = 108800,
    ) -> None:
        self._context_store = context_store
        self.tool_fold_threshold = tool_fold_threshold
        self.session_fold_threshold = session_fold_threshold
        self.tool_ratio_threshold = tool_ratio_threshold
        self.keep_recent_tool_rounds = keep_recent_tool_rounds
        self.keep_recent_dialog_rounds = keep_recent_dialog_rounds
        self.idle_timeout_seconds = idle_timeout_seconds
        self.single_result_char_limit = single_result_char_limit
        self.effective_window = effective_window

        self._fold_last_time: float = 0.0
        self._fold_count: int = 0
        self._tool_fold_count: int = 0
        self._session_fold_count: int = 0

    @property
    def context_store(self) -> ContextStore:
        return self._context_store

    async def run_pipeline(
        self,
        session: Any,
        last_input_token_count: int,
        last_api_call_time: float,
        side_query: SideQueryFn | None,
        session_id: str,
        folded_memories: list[dict],
    ) -> bool:
        """主入口：根据条件选择压缩策略。
        
        Returns:
            bool: 是否执行了压缩
        """
        utilization = last_input_token_count / self.effective_window if self.effective_window else 0
        tool_ratio = self._calculate_tool_ratio(session)
        idle_seconds = time.time() - last_api_call_time if last_api_call_time else 0

        folded = False

        # 策略 A：工具调用折叠
        should_tool_fold = (
            (utilization > self.tool_fold_threshold and tool_ratio > self.tool_ratio_threshold)
            or idle_seconds > self.idle_timeout_seconds
        )
        if should_tool_fold:
            folded = await self._fold_tool_results(session, side_query)

        # 策略 B：会话折叠
        should_session_fold = (
            utilization > self.session_fold_threshold and tool_ratio < self.tool_ratio_threshold
        )
        if should_session_fold:
            folded = await self._fold_session(session, side_query, session_id, folded_memories) or folded

        return folded

    async def compact_manual(
        self,
        session: Any,
        side_query: SideQueryFn | None,
        session_id: str,
        folded_memories: list[dict],
    ) -> bool:
        """手动触发会话折叠。"""
        if len(session._log) < 4:
            return False
        return await self._fold_session(session, side_query, session_id, folded_memories)

    def _calculate_tool_ratio(self, session: Any) -> float:
        """计算工具调用占比。"""
        tool_count = 0
        total_count = 0
        for event in session._log:
            t = event.get("type")
            if t in ("user_message", "assistant_message", "tool_result_msg"):
                total_count += 1
                if t == "tool_result_msg":
                    tool_count += 1
        return tool_count / total_count if total_count > 0 else 0

    async def _fold_tool_results(self, session: Any, side_query: SideQueryFn | None) -> bool:
        """策略 A：工具调用折叠。
        
        保留最近 N 轮工具调用，更早的替换为占位符，原文存入 ContextStore。
        使用 mark_tool_snipped() 写入标记事件，不修改已有事件。
        """
        # 收集已 snipped 的 seq
        already_snipped = set()
        for event in session._log:
            if event.get("type") == "context/tool_snipped":
                for item in event.get("snipped", []):
                    already_snipped.add(item["seq"])
        
        # 找到所有未 snipped 的工具结果
        tool_events = [
            e for e in session._log
            if e.get("type") == "tool_result_msg"
            and e.get("seq") not in already_snipped
        ]
        
        if len(tool_events) <= self.keep_recent_tool_rounds:
            return False
        
        to_fold = tool_events[:-self.keep_recent_tool_rounds]
        snipped_list = []
        
        for event in to_fold:
            content = event.get("content", "")
            call_id = event.get("call_id", "")
            key = f"snip:{call_id}"
            
            # 单条 > 30K：Side Model 摘要或截断
            if len(content) > self.single_result_char_limit:
                if side_query:
                    try:
                        abstract = await side_query(
                            "Summarize this tool result concisely:",
                            content[:50000]
                        )
                    except Exception:
                        abstract = None
                    if abstract:
                        self._context_store.store(key, content, abstract)
                    else:
                        keep = (self.single_result_char_limit - 80) // 2
                        abstract = content[:keep] + "\n[... truncated ...]\n" + content[-keep:]
                        self._context_store.store(key, content, abstract)
                else:
                    keep = (self.single_result_char_limit - 80) // 2
                    abstract = content[:keep] + "\n[... truncated ...]\n" + content[-keep:]
                    self._context_store.store(key, content, abstract)
            else:
                self._context_store.store(key, content)
            
            snipped_list.append({"seq": event["seq"], "key": key})
        
        # 写入标记事件
        if snipped_list:
            session.mark_tool_snipped(snipped_list, trigger="auto")
            self._tool_fold_count += 1
            self._record_fold_event()
            return True
        
        return False

    async def _fold_session(
        self,
        session: Any,
        side_query: SideQueryFn | None,
        session_id: str,
        folded_memories: list[dict],
    ) -> bool:
        """策略 B：会话折叠。
        
        保留最近 N 轮对话，更早的折叠成结构化摘要。
        使用 mark_deleted() 和 context/session_folded 事件，不修改已有事件。
        """
        # 先执行工具调用折叠
        await self._fold_tool_results(session, side_query)
        
        # 收集已删除的 seq
        deleted_seqs = set()
        for event in session._log:
            if event.get("type") == "context/events_deleted":
                deleted_seqs.update(event.get("deleted_seqs", []))
        
        # 找到所有未删除的对话事件
        dialog_events = [
            e for e in session._log
            if e.get("type") in ("user_message", "assistant_message", "tool_result_msg")
            and e.get("seq") not in deleted_seqs
        ]
        
        # 计算保留的轮次（1 轮 ≈ 1 user + 1 assistant + N tools）
        keep_count = self.keep_recent_dialog_rounds * 3
        if len(dialog_events) <= keep_count:
            return False
        
        to_fold = dialog_events[:-keep_count]
        
        # 构建对话文本
        transcript = self._build_transcript(to_fold)
        if not transcript.strip():
            return False
        
        # 生成摘要
        if side_query:
            try:
                from .session_memory import build_folding_user_prompt, parse_folded_memory
                raw = await side_query(FOLD_SESSION_MEMORY_SYSTEM, build_folding_user_prompt(transcript))
                memory = parse_folded_memory(raw)
                summary = format_folded_memory(memory)
            except Exception:
                summary = fallback_folded_memory(transcript)
                summary = format_folded_memory(summary)
        else:
            summary = format_folded_memory(fallback_folded_memory(transcript))
        
        # 标记被折叠的事件为删除
        deleted_seqs_to_mark = [e["seq"] for e in to_fold]
        session.mark_deleted(deleted_seqs_to_mark, trigger="auto")
        
        # 写入摘要事件
        session.append("context/session_folded", {
            "summary": summary,
            "trigger": "auto",
        })
        
        # 保存折叠记忆
        try:
            record = {
                "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "trigger": "auto",
                "session_id": session_id,
                "summary": summary,
            }
            folded_memories.append(record)
            await asyncio.to_thread(save_folded_session_memory, session_id, _sanitize_for_utf8(record))
        except Exception:
            pass
        
        self._session_fold_count += 1
        self._record_fold_event()
        return True

    def _build_transcript(self, events: list[dict]) -> str:
        """构建对话文本用于摘要。"""
        lines = []
        for e in events:
            t = e.get("type", "")
            if t == "user_message":
                role = "user"
            elif t == "assistant_message":
                role = "assistant"
            elif t == "tool_result_msg":
                role = "tool result"
            else:
                continue
            content = e.get("content", "")[:500]
            lines.append(f"[{role}]: {content}")
        return "\n".join(lines)

    def _record_fold_event(self) -> None:
        self._fold_last_time = time.time()
        self._fold_count += 1

    @property
    def last_fold_time_iso(self) -> str | None:
        if self._fold_last_time <= 0:
            return None
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self._fold_last_time))

    def get_stats(self) -> dict[str, Any]:
        return {
            "tool_fold": {
                "triggered": self._tool_fold_count,
            },
            "session_fold": {
                "triggered": self._session_fold_count,
            },
            "total_folds": {
                "triggered": self._fold_count,
                "last_fold_time": self.last_fold_time_iso,
            },
        }
