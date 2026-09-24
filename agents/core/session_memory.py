"""Session-scoped structured memory folding for conversation compaction."""

from __future__ import annotations

import json
from typing import Any



MAX_TRANSCRIPT_CHARS = 80_000
MAX_BLOCK_CHARS = 12_000



def clip_text(text: str, limit: int = MAX_BLOCK_CHARS) -> str:
    text = str(text or "")
    if len(text) <= limit:
        return text
    keep = max(100, (limit - 80) // 2)
    return text[:keep] + f"\n\n[... clipped {len(text) - keep * 2} chars ...]\n\n" + text[-keep:]


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                parts.append(str(block))
                continue
            btype = block.get("type")
            if btype == "text":
                parts.append(str(block.get("text") or ""))
            elif btype == "tool_result":
                parts.append(
                    "TOOL_RESULT"
                    f" id={block.get('tool_use_id', '')}\n"
                    f"{clip_text(str(block.get('content') or ''))}"
                )
            elif btype == "tool_use":
                parts.append(
                    "TOOL_USE"
                    f" id={block.get('id', '')}"
                    f" name={block.get('name', '')}"
                    f" input={json.dumps(block.get('input') or {}, ensure_ascii=False)}"
                )
            elif "content" in block:
                parts.append(str(block.get("content") or ""))
        return "\n".join(p for p in parts if p)
    return ""


def build_openai_transcript(messages: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for i, msg in enumerate(messages):
        if not isinstance(msg, dict):
            continue
        role = str(msg.get("role") or "unknown")
        if role == "system":
            continue
        lines = [f"## Message {i} ({role})"]
        content = _content_text(msg.get("content"))
        if content:
            lines.append(clip_text(content))
        tool_calls = msg.get("tool_calls")
        if isinstance(tool_calls, list) and tool_calls:
            for tc in tool_calls:
                if not isinstance(tc, dict):
                    continue
                fn = tc.get("function") or {}
                lines.append(
                    "TOOL_CALL"
                    f" id={tc.get('id', '')}"
                    f" name={fn.get('name', '')}"
                    f" arguments={fn.get('arguments', '')}"
                )
        if role == "tool":
            lines.append(f"tool_call_id={msg.get('tool_call_id', '')}")
        parts.append("\n".join(lines))
    return clip_text("\n\n".join(parts), MAX_TRANSCRIPT_CHARS)



def fallback_folded_memory(transcript: str) -> dict[str, Any]:
    return {
        "episode_memory": {
            "task_description": "Previous conversation was compacted without structured JSON.",
            "key_events": [],
            "current_progress": clip_text(transcript, 6000),
        },
        "working_memory": {
            "immediate_goal": "Continue the user's current coding task from the compacted context.",
            "current_challenges": "Some detail may have been lost during fallback compaction.",
            "next_actions": [{"type": "planning", "description": "Review the folded context and continue carefully."}],
        },
        "tool_memory": {"tools_used": [], "derived_rules": []},
    }


def format_folded_memory(memory: dict[str, Any]) -> str:
    return (
        "<session-folded-memory>\n"
        "Previous raw conversation history was compacted. Use this structured memory as session state, "
        "but verify file contents and live environment state before making code changes.\n\n"
        f"{json.dumps(memory, ensure_ascii=False, indent=2)}\n"
        "</session-folded-memory>\n\n"
        "Continue the task from this state."
    )


def format_tool_folded_summary(event: dict[str, Any]) -> str:
    abstracts = [a for a in event.get("abstracts", []) if isinstance(a, dict)]
    assistant_texts = [a for a in event.get("assistant_texts", []) if isinstance(a, dict)]
    if not abstracts and not assistant_texts:
        return ""

    entries: list[tuple[int, int, int, dict[str, Any]]] = []
    for index, text_entry in enumerate(assistant_texts):
        seq = text_entry.get("seq")
        sort_seq = seq if isinstance(seq, int) else index
        entries.append((0 if isinstance(seq, int) else 1, sort_seq, index, text_entry))
    for index, abstract in enumerate(abstracts):
        seq = abstract.get("seq")
        sort_seq = seq if isinstance(seq, int) else index
        entries.append((0 if isinstance(seq, int) else 1, sort_seq, index + len(assistant_texts), abstract))
    entries.sort(key=lambda item: (item[0], item[1], item[2]))

    lines = [
        "<folded-tool-results>",
        "Older tool calls were compacted. Use context_restore(key='snip:<call_id>') when the full result is needed.",
    ]
    for _, _, _, entry in entries:
        if "call_id" in entry:
            call_id = str(entry.get("call_id") or "")
            tool_name = str(entry.get("tool_name") or "")
            arguments = clip_text(str(entry.get("arguments") or ""), 500)
            header = f"- call_id={call_id or 'unknown'}"
            if tool_name:
                header += f" tool={tool_name}"
            if arguments:
                header += f" arguments={arguments}"
            lines.append(header)
            body = clip_text(str(entry.get("abstract") or ""), MAX_BLOCK_CHARS)
            lines.append("  result: " + body.replace("\n", "\n  "))
        else:
            seq = entry.get("seq")
            body = clip_text(str(entry.get("text") or ""), MAX_BLOCK_CHARS)
            lines.append(f"- assistant_text seq={seq if isinstance(seq, int) else 'unknown'}")
            lines.append("  " + body.replace("\n", "\n  "))
    lines.append("</folded-tool-results>")
    return "\n".join(lines)
