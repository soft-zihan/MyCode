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

from agents.core.text_sanitization import sanitize_for_utf8


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
            session._log.extend(sanitize_for_utf8(data["events"]))
        
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
        from .session import derive_messages_from_event

        mapping: dict[int, int] = {}
        msg_idx = 1 if session.system_prompt else 0
        for seq in session.visible_seqs:
            event = session.event_at(seq)
            message_count = len(derive_messages_from_event(event))
            for _ in range(message_count):
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


# ── Agent 级生命周期操作（U0 从 Agent 迁入；Agent 保留同名门面）──

def restore_agent_session(agent: Any, data: dict) -> None:
    from agents.logging import print_info

    state = SessionState(session_id=agent.session_id, model=agent.model)
    agent._session_lifecycle.restore(state, data, agent.session)
    print_info(f"Session restored ({agent._get_message_count()} messages).")


async def rewind_agent_turns(agent: Any, n: int = 1) -> str:
    """统一回退：对话回退 N 轮 + 文件恢复到快照（原子操作）。"""
    from agents.core.rewind_service import get_rewind_service
    svc = get_rewind_service()
    plan = await svc.stage(
        agent.session_id, turns=n, session=agent.session, workspace=str(agent.workspace)
    )
    result = await svc.commit(plan.id, session=agent.session)
    msg = (
        f"Rewound {n} turn(s): removed {result['removed_user_messages']} user messages, "
        f"{result['removed_events']} events"
    )
    if result["restored_files"]:
        msg += f", restored {len(result['restored_files'])} files"
    return msg


def fork_agent_session(agent: Any) -> str:
    state = SessionState(
        session_id=agent.session_id,
        model=agent.model,
        start_time=agent.session_start_time,
        cwd=str(agent.workspace),
    )
    result, new_session = agent._session_lifecycle.fork(state, agent.session)
    agent.session = new_session
    agent.session_id = new_session.id
    return result
