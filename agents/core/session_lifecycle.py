"""SessionLifecycle — Session 持久化、恢复、Fork、Rewind。

职责：
- Session 序列化/反序列化
- 上下文编辑（/context, /ctx del, /ctx keep）
- Session fork（深拷贝分支）
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

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
    """Session 状态（简化版，只保留事件日志）。"""
    session_id: str
    model: str
    context_store: ContextStore
    start_time: str = ""
    events: list[dict] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.start_time:
            self.start_time = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class SessionLifecycle:
    def __init__(
        self,
        context_store: ContextStore,
    ) -> None:
        self._context_store = context_store

    @property
    def context_store(self) -> ContextStore:
        return self._context_store

    # ── 恢复 ──

    def restore(self, state: SessionState, data: dict, session: Any) -> None:
        """从事件日志恢复 Session 状态。"""
        # 恢复事件日志
        if isinstance(data.get("events"), list):
            session._log.clear()
            session._log.extend(_sanitize_for_utf8(data["events"]))
        # 恢复上下文存储
        if isinstance(data.get("contextStore"), dict):
            self._context_store.restore_state(data["contextStore"])

    # ── Rewind ──

    def rewind(self, session: Any, read_file_state: dict, n: int = 1) -> str:
        """回退 N 个轮次。"""
        # 找到最近的 n 个 turn/start 事件
        turn_starts = [i for i, e in enumerate(session._log) if e.get("type") == "turn/start"]
        if len(turn_starts) < n:
            return f"Cannot rewind {n} turns; only {len(turn_starts)} turns recorded."
        
        # 截断到第 n 个 turn/start 之前
        target_idx = turn_starts[-n]
        session._log = session._log[:target_idx]
        
        return f"Rewound {n} turn(s). Events now: {len(session._log)}."

    # ── Fork ──

    def fork(self, state: SessionState, session: Any) -> tuple[str, Any]:
        """Fork 当前 Session，返回 (new_session_id, new_session)。"""
        from .session import Session
        
        old_id = state.session_id
        
        # 创建新 Session
        new_session = Session()
        new_session._log = json.loads(json.dumps(session._log, default=str))
        new_session.system_prompt = session.system_prompt
        
        # 复制上下文存储
        new_store = ContextStore()
        new_store.restore_state(json.loads(json.dumps(self._context_store.to_dict(), default=str)))
        
        new_state = SessionState(
            session_id=new_session.id,
            model=state.model,
            context_store=new_store,
            start_time=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        )
        
        self._save_state(new_state, new_session)
        return f"Forked session {old_id} -> {new_session.id}. You are now on the new branch.", new_session

    # ── 上下文编辑 ──

    def describe(self, messages: list[dict]) -> list[dict]:
        from .context_edit import describe_messages
        return describe_messages(messages, True)

    def delete_messages(self, session: Any, indexes: list[int]) -> str:
        """删除指定 index 的消息组。
        
        使用 mark_deleted() 写入标记事件，不重建事件日志。
        """
        from .context_edit import message_group
        
        # 建立 message index -> event seq 的映射
        messages = session.get_messages_for_llm()
        index_to_seqs = {}
        msg_idx = 0
        for event in session._log:
            t = event.get("type")
            if t in ("user_message", "assistant_message", "tool_result_msg"):
                index_to_seqs[msg_idx] = event.get("seq")
                msg_idx += 1
        
        # 找出要删除的 seq
        seqs_to_delete = []
        for idx in sorted(set(indexes)):
            if idx < 0 or idx >= len(messages):
                continue
            # 找出该 index 对应的消息组
            group = message_group(messages, idx, True)
            for group_idx in group:
                if group_idx in index_to_seqs:
                    seqs_to_delete.append(index_to_seqs[group_idx])
        
        # 标记为删除
        session.mark_deleted(seqs_to_delete, trigger="manual")
        
        return f"Deleted {len(seqs_to_delete)} message(s)."
    
    def keep_messages(self, session: Any, indexes: list[int]) -> str:
        """只保留指定 index 的消息组。
        
        使用 mark_deleted() 写入标记事件，不重建事件日志。
        """
        from .context_edit import message_group
        
        # 建立 message index -> event seq 的映射
        messages = session.get_messages_for_llm()
        index_to_seqs = {}
        msg_idx = 0
        for event in session._log:
            t = event.get("type")
            if t in ("user_message", "assistant_message", "tool_result_msg"):
                index_to_seqs[msg_idx] = event.get("seq")
                msg_idx += 1
        
        # 找出要保留的 seq
        seqs_to_keep = set()
        for idx in sorted(set(indexes)):
            if idx < 0 or idx >= len(messages):
                continue
            group = message_group(messages, idx, True)
            for group_idx in group:
                if group_idx in index_to_seqs:
                    seqs_to_keep.add(index_to_seqs[group_idx])
        
        # 找出要删除的 seq（所有消息 seq - 要保留的 seq）
        all_msg_seqs = set(index_to_seqs.values())
        seqs_to_delete = list(all_msg_seqs - seqs_to_keep)
        
        # 标记为删除
        session.mark_deleted(seqs_to_delete, trigger="manual")
        
        return f"Kept {len(seqs_to_keep)} message(s), removed {len(seqs_to_delete)}."

    # ── 序列化 ──

    def to_dict(self) -> dict:
        return {
            "contextStore": self._context_store.to_dict(),
        }

    def _save_state(self, state: SessionState, session: Any) -> None:
        save_session(state.session_id, {
            "metadata": {
                "id": state.session_id,
                "model": state.model,
                "cwd": str(Path.cwd()),
                "startTime": state.start_time,
                "messageCount": len(session.get_messages_for_llm()),
            },
            "parent_session": session.parent_session,
            "origin": session.origin,
            "agent_type": session.agent_type,
            "events": _sanitize_for_utf8(session._log),
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
