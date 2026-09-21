from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from .context_edit import estimate_message_tokens
from .session import derive_messages_from_event
from .session_memory import build_openai_transcript, clip_text

MESSAGE_EVENT_TYPES = frozenset({
    "user_message",
    "memory_injection",
    "assistant_message",
    "tool_result_msg",
    "tool_folded",
    "session_folded",
})

TOOL_GROUP_KINDS = frozenset({"tool_call", "tool_result", "tool_folded"})


def collect_hidden_seqs(events: Iterable[dict[str, Any]]) -> set[int]:
    hidden: set[int] = set()
    for event in events:
        if event.get("type") != "events_hidden":
            continue
        for seq in event.get("hidden_seqs") or []:
            try:
                hidden.add(int(seq))
            except (TypeError, ValueError):
                continue
    return hidden


@dataclass
class EventGroup:
    kind: str
    events: list[dict[str, Any]] = field(default_factory=list)

    @property
    def seqs(self) -> list[int]:
        return [event["seq"] for event in self.events if "seq" in event]

    @property
    def is_tool(self) -> bool:
        return self.kind in TOOL_GROUP_KINDS

    @property
    def is_user(self) -> bool:
        return self.kind == "user"


def visible_message_events(session: Any) -> list[dict[str, Any]]:
    return [
        event for event in session.visible_events
        if event.get("type") in MESSAGE_EVENT_TYPES
    ]


def estimate_event_tokens(event: dict[str, Any]) -> int:
    return sum(estimate_message_tokens(msg) for msg in derive_messages_from_event(event))


def estimate_visible_message_tokens(session: Any) -> int:
    return sum(estimate_event_tokens(event) for event in visible_message_events(session))


def estimate_tokens_after_seq(session: Any, seq: int) -> int:
    if seq < 0:
        return 0
    return sum(
        estimate_event_tokens(session.event_at(visible_seq))
        for visible_seq in session.visible_seqs
        if visible_seq > seq
    )


def build_message_groups(session: Any) -> list[EventGroup]:
    groups: list[EventGroup] = []
    owners: dict[str, EventGroup] = {}

    for event in visible_message_events(session):
        event_type = event.get("type")

        if event_type == "assistant_message":
            tool_calls = event.get("tool_calls") or []
            if tool_calls:
                group = EventGroup("tool_call", [event])
                groups.append(group)
                for tool_call in tool_calls:
                    if not isinstance(tool_call, dict):
                        continue
                    call_id = tool_call.get("id")
                    if call_id:
                        owners[str(call_id)] = group
            else:
                groups.append(EventGroup("assistant_text", [event]))
        elif event_type == "tool_result_msg":
            owner = owners.get(str(event.get("call_id") or ""))
            if owner is not None:
                owner.events.append(event)
            else:
                groups.append(EventGroup("tool_result", [event]))
        elif event_type == "user_message":
            groups.append(EventGroup("user", [event]))
        elif event_type == "memory_injection":
            groups.append(EventGroup("memory", [event]))
        elif event_type == "tool_folded":
            groups.append(EventGroup("tool_folded", [event]))
        elif event_type == "session_folded":
            groups.append(EventGroup("session_folded", [event]))

    return groups


def group_messages(groups: list[EventGroup]) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    for group in groups:
        for event in group.events:
            messages.extend(derive_messages_from_event(event))
    return messages


def build_groups_transcript(groups: list[EventGroup]) -> str:
    return build_openai_transcript(group_messages(groups))


def find_tool_fold_indices(groups: list[EventGroup], keep_recent_tool_rounds: int) -> list[int]:
    tool_indices = [index for index, group in enumerate(groups) if group.is_tool]
    if len(tool_indices) <= keep_recent_tool_rounds:
        return []
    return tool_indices[:-keep_recent_tool_rounds]


def find_session_fold_cut(
    groups: list[EventGroup],
    keep_recent_dialog_rounds: int,
    keep_recent_trajectory_tool_rounds: int,
) -> int:
    user_indices = [index for index, group in enumerate(groups) if group.is_user]
    if len(user_indices) > keep_recent_dialog_rounds:
        return user_indices[-keep_recent_dialog_rounds]

    tool_indices = [index for index, group in enumerate(groups) if group.is_tool]
    if len(tool_indices) <= keep_recent_trajectory_tool_rounds:
        return -1

    cut = tool_indices[-keep_recent_trajectory_tool_rounds]
    assistant_text_indices = [
        index for index, group in enumerate(groups) if group.kind == "assistant_text"
    ]
    if assistant_text_indices:
        cut = min(cut, assistant_text_indices[-1])
    if user_indices:
        cut = max(cut, user_indices[-1] + 1)
    if cut <= 0 or cut >= len(groups):
        return -1
    if all(group.is_user for group in groups[:cut]):
        return -1
    return cut


def latest_session_notes(session: Any, limit: int = 20_000) -> str:
    for event in reversed(session.events):
        if event.get("type") != "session_folded":
            continue
        notes = str(event.get("session_notes") or "").strip()
        if not notes:
            notes = str(event.get("summary") or "").strip()
        return clip_text(notes, limit)
    return ""
