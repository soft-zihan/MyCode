"""SessionLifecycle — Session 持久化、恢复、Fork、Rewind。

职责：
- Session 序列化/反序列化
- 上下文编辑（/context, /ctx del、/ctx keep）
- Session fork（深拷贝分支）
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


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
    start_time: str = ""
    cwd: str = ""
    events: list[dict] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.start_time:
            self.start_time = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class SessionLifecycle:
    def __init__(self) -> None:
        pass

    # ── 恢复 ──

    def restore(self, state: SessionState, data: dict, session: Any) -> None:
        """从事件日志恢复 Session 状态。"""
        if isinstance(data.get("events"), list):
            session._log.clear()
            session._log.extend(_sanitize_for_utf8(data["events"]))
        
        # 恢复 plan_slug
        if data.get("plan_slug"):
            session.plan_slug = data["plan_slug"]

    # ── Fork ──

    def fork(self, state: SessionState, session: Any) -> tuple[str, Any]:
        """Fork 当前 Session，返回 (new_session_id, new_session)。"""
        from .session import Session
        
        old_id = state.session_id
        
        new_session = Session()
        new_session._log = json.loads(json.dumps(session._log, default=str))
        new_session.system_prompt = session.system_prompt
        
        new_state = SessionState(
            session_id=new_session.id,
            model=state.model,
            start_time=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            cwd=state.cwd,
        )
        
        return f"Forked session {old_id} -> {new_session.id}. You are now on the new branch.", new_session

    # ── 上下文编辑 ──

    def describe(self, messages: list[dict]) -> list[dict]:
        from .context_edit import describe_messages
        return describe_messages(messages, True)

    def _message_index_to_seqs(self, session: Any) -> dict[int, int]:
        """显示消息索引 → 事件 seq 映射（与 get_messages_for_llm 生成逻辑严格一致）。

        - system prompt 占 index 0：非事件、不进映射，天然不可删除
        - 只统计可见事件：已隐藏事件不产生消息，索引必须按可见序列计
        - tool_folded 产生 len(abstracts) 条消息（共享同一 seq）
        - session_folded / user_message / assistant_message / tool_result_msg / memory_injection 各产生 1 条
        """
        mapping: dict[int, int] = {}
        msg_idx = 1 if session.system_prompt else 0
        for seq in session._visible_seqs:
            event = session._log[seq]
            t = event.get("type")
            if t == "tool_folded":
                for _ in event.get("abstracts", []):
                    mapping[msg_idx] = seq
                    msg_idx += 1
            elif t in ("session_folded", "user_message", "assistant_message", "tool_result_msg", "memory_injection"):
                mapping[msg_idx] = seq
                msg_idx += 1
        return mapping

    def delete_messages(self, session: Any, indexes: list[int]) -> str:
        """删除指定 index 的消息组。
        
        通过追加 events_hidden 事件隐藏事件。
        """
        from .context_edit import message_group
        
        messages = session.get_messages_for_llm()
        index_to_seqs = self._message_index_to_seqs(session)
        
        seqs_to_delete: list[int] = []
        for idx in sorted(set(indexes)):
            if idx < 0 or idx >= len(messages):
                continue
            group = message_group(messages, idx, True)
            for group_idx in group:
                if group_idx in index_to_seqs:
                    seqs_to_delete.append(index_to_seqs[group_idx])
        
        seqs_to_delete = list(dict.fromkeys(seqs_to_delete))  # tool_folded 多索引共享 seq，去重
        session.hide_events(seqs_to_delete)
        
        return f"Deleted {len(seqs_to_delete)} message(s)."
    
    def keep_messages(self, session: Any, indexes: list[int]) -> str:
        """只保留指定 index 的消息组。
        
        通过追加 events_hidden 事件隐藏其余事件。
        """
        from .context_edit import message_group
        
        messages = session.get_messages_for_llm()
        index_to_seqs = self._message_index_to_seqs(session)
        
        seqs_to_keep = set()
        for idx in sorted(set(indexes)):
            if idx < 0 or idx >= len(messages):
                continue
            group = message_group(messages, idx, True)
            for group_idx in group:
                if group_idx in index_to_seqs:
                    seqs_to_keep.add(index_to_seqs[group_idx])
        
        all_msg_seqs = set(index_to_seqs.values())
        seqs_to_delete = list(all_msg_seqs - seqs_to_keep)
        
        session.hide_events(seqs_to_delete)
        
        return f"Kept {len(seqs_to_keep)} message(s), removed {len(seqs_to_delete)}."
