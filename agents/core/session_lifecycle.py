"""SessionLifecycle — Session 持久化、恢复、Fork、Rewind。

职责：
- Session 序列化/反序列化
- 轮次边界管理（用于 /rewind）
- 上下文编辑（/context, /ctx del, /ctx keep）
- Session fork（深拷贝分支）
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .checkpoints import FileCheckpointStore, TurnBoundary
from .context_store import ContextStore
from .session import save_session


def _sanitize_for_utf8(value: Any) -> Any:
    if isinstance(value, str):
        return value.encode("utf-8", errors="replace").decode("utf-8")
    if isinstance(value, list):
        return [_sanitize_for_utf8(v) for v in value]
    if isinstance(value, dict):
        return {k: _sanitize_for_utf8(v) for k, v in value.items()}
    return value


@dataclass
class SessionState:
    session_id: str
    model: str
    messages: list[dict]
    folded_memories: list[dict]
    turn_boundaries: list[TurnBoundary]
    checkpoint_store: FileCheckpointStore
    context_store: ContextStore
    start_time: str = ""

    def __post_init__(self) -> None:
        if not self.start_time:
            self.start_time = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class SessionLifecycle:
    def __init__(
        self,
        checkpoint_store: FileCheckpointStore,
        context_store: ContextStore,
    ) -> None:
        self._checkpoint_store = checkpoint_store
        self._context_store = context_store
        self._turn_boundaries: list[TurnBoundary] = []

    @property
    def turn_boundaries(self) -> list[TurnBoundary]:
        return self._turn_boundaries

    @property
    def checkpoint_store(self) -> FileCheckpointStore:
        return self._checkpoint_store

    @property
    def context_store(self) -> ContextStore:
        return self._context_store

    # ── 轮次边界 ──

    def record_boundary(self, turn: int, message_count: int, checkpoint_count: int) -> None:
        self._turn_boundaries.append(TurnBoundary(
            turn=turn,
            message_count=message_count,
            checkpoint_count=checkpoint_count,
        ))

    # ── 恢复 ──

    def restore(self, state: SessionState, data: dict) -> None:
        if data.get("openaiMessages"):
            state.messages.clear()
            state.messages.extend(_sanitize_for_utf8(data["openaiMessages"]))
        if isinstance(data.get("foldedSessionMemories"), list):
            state.folded_memories.clear()
            state.folded_memories.extend(_sanitize_for_utf8(data["foldedSessionMemories"]))
        if isinstance(data.get("checkpointStore"), dict):
            self._checkpoint_store.restore_state(data["checkpointStore"])
        if isinstance(data.get("turnBoundaries"), list):
            self._turn_boundaries = [
                TurnBoundary(**b) for b in data["turnBoundaries"]
                if isinstance(b, dict) and {"turn", "message_count", "checkpoint_count"} <= set(b)
            ]
        if isinstance(data.get("contextStore"), dict):
            self._context_store.restore_state(data["contextStore"])

    # ── Rewind ──

    def rewind(self, state: SessionState, read_file_state: dict, n: int = 1) -> str:
        if not self._turn_boundaries:
            return "Nothing to rewind (no completed turns yet)."
        n = max(1, n)
        idx = len(self._turn_boundaries) - n
        if idx < 0:
            return f"Cannot rewind {n} turns; only {len(self._turn_boundaries)} turns recorded."

        boundary = self._turn_boundaries[idx]
        state.messages = state.messages[:boundary.message_count]
        file_results = self._checkpoint_store.restore_after(boundary.checkpoint_count)
        for path_key in file_results:
            read_file_state.pop(path_key, None)
        self._turn_boundaries = self._turn_boundaries[:idx]

        restored = sum(1 for v in file_results.values() if v == "restored")
        deleted = sum(1 for v in file_results.values() if v == "deleted")
        parts = [f"Rewound {n} turn(s). Messages now: {len(state.messages)}."]
        if restored:
            parts.append(f"Restored {restored} file(s).")
        if deleted:
            parts.append(f"Deleted {deleted} newly-created file(s).")
        return " ".join(parts)

    # ── Fork ──

    def fork(self, state: SessionState) -> str:
        old_id = state.session_id
        self._save_state(state)

        new_id = uuid.uuid4().hex[:8]
        state.session_id = new_id
        state.start_time = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

        from agents.observability.trace import set_trace_session
        set_trace_session(new_id)

        state.messages = json.loads(json.dumps(state.messages, default=str))
        state.folded_memories = json.loads(json.dumps(state.folded_memories, default=str))
        self._turn_boundaries = [TurnBoundary(b.turn, b.message_count, b.checkpoint_count) for b in self._turn_boundaries]
        self._checkpoint_store = self._checkpoint_store.fork(new_id)

        new_store = ContextStore()
        new_store.restore_state(json.loads(json.dumps(self._context_store.to_dict(), default=str)))
        self._context_store = new_store
        state.context_store = self._context_store

        self._save_state(state)
        return f"Forked session {old_id} -> {new_id}. You are now on the new branch."

    # ── 上下文编辑 ──

    def describe(self, messages: list[dict]) -> list[dict]:
        from .context_edit import describe_messages
        return describe_messages(messages, True)

    def delete_messages(self, state: SessionState, indexes: list[int]) -> str:
        from .context_edit import delete_message_group
        messages = state.messages
        referenced_before = _referenced_store_keys(messages)
        total_deleted = 0
        for idx in sorted(set(indexes)):
            messages, deleted = delete_message_group(messages, idx, True)
            total_deleted += deleted
        state.messages = messages
        _drop_orphaned_store_entries(self._context_store, referenced_before, messages)
        return f"Deleted {total_deleted} message(s). Context now: {len(state.messages)} messages."

    def keep_messages(self, state: SessionState, indexes: list[int]) -> str:
        from .context_edit import keep_only_groups
        messages = state.messages
        referenced_before = _referenced_store_keys(messages)
        kept, deleted = keep_only_groups(messages, indexes, True)
        state.messages = kept
        _drop_orphaned_store_entries(self._context_store, referenced_before, kept)
        return f"Kept {len(kept)} message(s), removed {deleted}. Context now: {len(state.messages)} messages."

    # ── 序列化 ──

    def to_dict(self) -> dict:
        return {
            "turnBoundaries": [
                {"turn": b.turn, "message_count": b.message_count, "checkpoint_count": b.checkpoint_count}
                for b in self._turn_boundaries
            ],
            "checkpointStore": self._checkpoint_store.to_dict(),
            "contextStore": self._context_store.to_dict(),
        }

    def _save_state(self, state: SessionState) -> None:
        save_session(state.session_id, {
            "metadata": {
                "id": state.session_id,
                "model": state.model,
                "cwd": str(Path.cwd()),
                "startTime": state.start_time,
                "messageCount": len(state.messages),
            },
            "openaiMessages": _sanitize_for_utf8(state.messages),
            "foldedSessionMemories": _sanitize_for_utf8(state.folded_memories),
            "checkpointStore": self._checkpoint_store.to_dict(),
            "turnBoundaries": [
                {"turn": b.turn, "message_count": b.message_count, "checkpoint_count": b.checkpoint_count}
                for b in self._turn_boundaries
            ],
            "contextStore": self._context_store.to_dict(),
        })


def _referenced_store_keys(messages: list[dict]) -> set[str]:
    import re as _re
    referenced: set[str] = set()
    for msg in messages:
        content = msg.get("content")
        texts: list[str] = []
        if isinstance(content, str):
            texts.append(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and isinstance(block.get("content"), str):
                    texts.append(block["content"])
        for text in texts:
            referenced.update(_re.findall(r"key '([^']+)'", text))
    return referenced


def _drop_orphaned_store_entries(context_store: ContextStore, referenced_before: set[str], kept_messages: list[dict]) -> None:
    referenced_after = _referenced_store_keys(kept_messages)
    for key in referenced_before - referenced_after:
        context_store.mark_dropped(key)
