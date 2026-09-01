"""ContextCompressor — 多层级上下文压缩管线。

职责：
- L1 预算截断（利用率 >50%）
- L2 Snip（利用率 >60%，原文存入 ContextStore，可恢复）
- L3 Microcompact（空闲 >5min，原文存入 ContextStore，可恢复）
- L4 语义 compact（利用率 >85%，LLM 折叠，不可逆）
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Callable, Awaitable

from .context_store import (
    ContextStore,
    is_compressed_placeholder,
    snipped_placeholder,
    cleared_placeholder,
)
from .session_memory import (
    build_openai_transcript,
    build_folding_user_prompt,
    parse_folded_memory,
    fallback_folded_memory,
    format_folded_memory,
    FOLD_SESSION_MEMORY_SYSTEM,
)
from .session import save_session, save_folded_session_memory
from agents.ui import print_info

SNIP_THRESHOLD = 0.60
MICROCOMPACT_IDLE_S = 5 * 60
KEEP_RECENT_RESULTS = 3

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
        auto_compact_threshold: float = 0.85,
        effective_window: int = 108800,
    ) -> None:
        self._context_store = context_store
        self.auto_compact_threshold = auto_compact_threshold
        self.effective_window = effective_window

        self._fold_last_time: float = 0.0
        self._fold_count: int = 0

    @property
    def context_store(self) -> ContextStore:
        return self._context_store

    # ── 管线入口 ──

    def run_pipeline(self, messages: list[dict], last_input_token_count: int, last_api_call_time: float) -> None:
        self._budget(messages, last_input_token_count)
        self._snip(messages, last_input_token_count)
        self._microcompact(messages, last_api_call_time)

    async def check_and_compact(
        self,
        messages: list[dict],
        last_input_token_count: int,
        side_query: SideQueryFn | None,
        session_id: str,
        folded_memories: list[dict],
        refresh_system_prompt: Callable[[], None],
        get_message_count: Callable[[], int],
    ) -> bool:
        if last_input_token_count > self.effective_window * self.auto_compact_threshold:
            print_info("Context window filling up, compacting conversation...")
            return await self.compact(
                messages, "auto", side_query, session_id,
                folded_memories, refresh_system_prompt, get_message_count,
            )
        return False

    async def compact(
        self,
        messages: list[dict],
        trigger: str,
        side_query: SideQueryFn | None,
        session_id: str,
        folded_memories: list[dict],
        refresh_system_prompt: Callable[[], None],
        get_message_count: Callable[[], int],
    ) -> bool:
        if len(messages) < 4:
            return False
        system_msg = messages[0]
        transcript = build_openai_transcript(_sanitize_for_utf8(messages))
        if not transcript.strip():
            return False

        memory = await self._generate_folded_memory(transcript, side_query)
        await self._record_folded_memory(trigger, memory, session_id, folded_memories)
        self._record_fold_event()

        messages.clear()
        messages.append(system_msg)
        messages.append({"role": "user", "content": format_folded_memory(memory)})

        refresh_system_prompt()
        return True

    # ── L1: 预算截断 ──

    def _budget(self, messages: list[dict], last_input_token_count: int) -> None:
        utilization = last_input_token_count / self.effective_window if self.effective_window else 0
        if utilization < 0.5:
            return
        budget = 15000 if utilization > 0.7 else 30000
        for msg in messages:
            if msg.get("role") == "tool" and isinstance(msg.get("content"), str) and len(msg["content"]) > budget:
                keep = (budget - 80) // 2
                msg["content"] = (
                    msg["content"][:keep]
                    + f"\n\n[... budgeted: {len(msg['content']) - keep * 2} chars truncated ...]\n\n"
                    + msg["content"][-keep:]
                )

    # ── L2: Snip ──

    def _snip(self, messages: list[dict], last_input_token_count: int) -> None:
        utilization = last_input_token_count / self.effective_window if self.effective_window else 0
        if utilization < SNIP_THRESHOLD:
            return
        tool_msgs = []
        for i, msg in enumerate(messages):
            if msg.get("role") == "tool" and isinstance(msg.get("content"), str) and not is_compressed_placeholder(msg["content"]):
                tool_msgs.append(i)
        if len(tool_msgs) <= KEEP_RECENT_RESULTS:
            return
        snip_count = len(tool_msgs) - KEEP_RECENT_RESULTS
        for i in range(snip_count):
            msg = messages[tool_msgs[i]]
            original = msg["content"]
            key = f"snip:{msg.get('tool_call_id') or tool_msgs[i]}"
            self._context_store.store(key, original)
            msg["content"] = snipped_placeholder(key, self._context_store.get_abstract(key))

    # ── L3: Microcompact ──

    def _microcompact(self, messages: list[dict], last_api_call_time: float) -> None:
        if not last_api_call_time or (time.time() - last_api_call_time) < MICROCOMPACT_IDLE_S:
            return
        tool_msgs = []
        for i, msg in enumerate(messages):
            if msg.get("role") == "tool" and isinstance(msg.get("content"), str) and not is_compressed_placeholder(msg["content"]):
                tool_msgs.append(i)
        clear_count = len(tool_msgs) - KEEP_RECENT_RESULTS
        for i in range(max(0, clear_count)):
            msg = messages[tool_msgs[i]]
            original = msg["content"]
            key = f"clear:{msg.get('tool_call_id') or tool_msgs[i]}"
            self._context_store.store(key, original)
            msg["content"] = cleared_placeholder(key)

    # ── L4: 语义折叠 ──

    async def _generate_folded_memory(self, transcript: str, side_query: SideQueryFn | None) -> dict[str, Any]:
        if side_query is None:
            return fallback_folded_memory(transcript)
        try:
            raw = await side_query(FOLD_SESSION_MEMORY_SYSTEM, build_folding_user_prompt(transcript))
            return parse_folded_memory(raw)
        except Exception:
            return fallback_folded_memory(transcript)

    async def _record_folded_memory(self, trigger: str, memory: dict[str, Any], session_id: str, folded_memories: list[dict]) -> None:
        record = {
            "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "trigger": trigger,
            "session_id": session_id,
            **memory,
        }
        folded_memories.append(record)
        try:
            await asyncio.to_thread(save_folded_session_memory, session_id, _sanitize_for_utf8(record))
        except Exception:
            pass

    def _record_fold_event(self) -> None:
        self._fold_last_time = time.time()
        self._fold_count += 1
