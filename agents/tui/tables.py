"""Tables — OpenCode-aligned design."""

from __future__ import annotations

import json
from pathlib import Path

from rich import box
from rich.table import Table

from .output import console, _safe_text
from .theme import theme

_TRACE_KIND_STYLE = {
    "turn.start": (f"bold {theme.SECONDARY}", ["user_preview"]),
    "turn.end": (theme.SECONDARY, ["assistant_preview", "aborted"]),
    "tool.start": (f"bold {theme.WARNING}", ["tool", "input"]),
    "tool.end": (theme.WARNING, ["tool", "result_preview", "error"]),
    "bg.start": (f"bold {theme.ACCENT}", ["job_id", "command"]),
    "bg.done": (theme.ACCENT, ["job_id", "exit_code", "command"]),
    "compact": (f"bold {theme.SUCCESS}", ["trigger", "messages_after"]),
}


def print_context_rows(rows: list[dict]) -> None:
    table = Table(box=box.SIMPLE, header_style=f"bold {theme.SECONDARY}", border_style=theme.BORDER)
    table.add_column("#", style=f"bold {theme.PRIMARY}", no_wrap=True)
    table.add_column("Role", style=theme.SECONDARY, no_wrap=True)
    table.add_column("Content", style=theme.TEXT)
    table.add_column("Tokens", style=theme.TEXT_MUTED, justify="right", no_wrap=True)
    total = 0
    for r in rows:
        tokens = int(r.get("tokens", 0))
        total += tokens
        table.add_row(
            str(r.get("index", "")),
            _safe_text(r.get("role", "")),
            _safe_text(r.get("label", "")),
            str(tokens),
        )
    console.print(table)
    console.print(f"  [{theme.TEXT_MUTED}]{len(rows)} messages, ~{total} tokens[/{theme.TEXT_MUTED}]")


def print_memory_entries(memories: list[object]) -> None:
    table = Table(box=box.SIMPLE, header_style=f"bold {theme.SECONDARY}", border_style=theme.BORDER)
    table.add_column("Type", style=f"bold {theme.PRIMARY}", no_wrap=True)
    table.add_column("Name", style=theme.TEXT)
    table.add_column("Description", style=theme.TEXT_MUTED)
    for m in memories:
        table.add_row(
            _safe_text(getattr(m, "type", "")),
            _safe_text(getattr(m, "name", "")),
            _safe_text(getattr(m, "description", "")),
        )
    console.print(table)


def _shorten_path(path: str, max_len: int = 44) -> str:
    home = str(Path.home())
    if path.startswith(home):
        path = "~" + path[len(home):]
    if len(path) <= max_len:
        return path
    parts = path.split("/")
    kept: list[str] = []
    for seg in reversed(parts):
        candidate = ("/".join([seg] + kept))
        if len(candidate) + 1 > max_len and kept:
            break
        kept.insert(0, seg)
    return "…/" + "/".join(kept)


def _relative_time(iso_time: str) -> str:
    try:
        from datetime import datetime, timezone
        t = datetime.strptime(iso_time, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        delta = (datetime.now(timezone.utc) - t).total_seconds()
    except (ValueError, TypeError):
        return iso_time or "?"
    if delta < 60:
        return "just now"
    if delta < 3600:
        return f"{int(delta // 60)}m ago"
    if delta < 86400:
        return f"{int(delta // 3600)}h ago"
    return f"{int(delta // 86400)}d ago"


def print_session_rows(sessions: list[dict], current_id: str | None = None) -> None:
    table = Table(box=box.SIMPLE, header_style=f"bold {theme.SECONDARY}", border_style=theme.BORDER)
    table.add_column("", no_wrap=True)
    table.add_column("ID", style=f"bold {theme.PRIMARY}", no_wrap=True)
    table.add_column("When", style=theme.TEXT_MUTED, no_wrap=True)
    table.add_column("Msgs", justify="right", no_wrap=True)
    table.add_column("Model", style=theme.TEXT_MUTED, no_wrap=True)
    table.add_column("Working dir", style=theme.TEXT)
    for s in sessions:
        sid = s.get("id", "")
        is_current = sid == current_id
        marker = f"[{theme.PRIMARY}]●[/{theme.PRIMARY}]" if is_current else ""
        sid_style = f"bold {theme.PRIMARY}" if is_current else theme.PRIMARY
        table.add_row(
            marker,
            f"[{sid_style}]{_safe_text(sid)}[/{sid_style}]",
            _relative_time(s.get("startTime", "")),
            str(s.get("messageCount", 0)),
            _safe_text(s.get("model", "")),
            _safe_text(_shorten_path(s.get("cwd", ""))),
        )
    console.print(table)
    console.print(f"  [{theme.TEXT_MUTED}]{len(sessions)} sessions[/{theme.TEXT_MUTED}]")


def _trace_summary(kind: str, event: dict, max_len: int = 60) -> str:
    _, fields = _TRACE_KIND_STYLE.get(kind, ("white", []))
    parts = []
    for f in fields:
        if f in event and event[f] not in (None, ""):
            v = event[f]
            if isinstance(v, bool):
                parts.append(f"{f}={v}")
            else:
                parts.append(str(v))
    if not parts:
        rest = {k: v for k, v in event.items() if k not in ("ts", "kind")}
        parts.append(json.dumps(rest, ensure_ascii=False, default=str))
    text = " · ".join(parts).replace("\n", " ")
    return text if len(text) <= max_len else text[:max_len] + "…"


def print_trace_rows(events: list[dict], trace_file: str, enabled: bool) -> None:
    table = Table(box=box.SIMPLE, header_style=f"bold {theme.SECONDARY}", border_style=theme.BORDER)
    table.add_column("Time", style=theme.TEXT_MUTED, no_wrap=True)
    table.add_column("Event", no_wrap=True)
    table.add_column("Dur", justify="right", no_wrap=True, style=theme.TEXT_MUTED)
    table.add_column("Detail", style=theme.TEXT)
    for e in events:
        kind = e.get("kind", "?")
        style, _ = _TRACE_KIND_STYLE.get(kind, ("white", []))
        ts = e.get("ts", "")[11:23]
        dur = e.get("duration_s")
        table.add_row(
            _safe_text(ts),
            f"[{style}]{_safe_text(kind)}[/{style}]",
            f"{dur}s" if dur is not None else "",
            _safe_text(_trace_summary(kind, e)),
        )
    state = f"[bold {theme.SUCCESS}]ON[/bold {theme.SUCCESS}]" if enabled else f"[{theme.TEXT_MUTED}]OFF[/{theme.TEXT_MUTED}]"
    console.print(table)
    console.print(f"  [{theme.TEXT_MUTED}]{len(events)} events, logging {state}[/{theme.TEXT_MUTED}]")


def print_skill_entries(skills: list[object]) -> None:
    table = Table(box=box.SIMPLE, header_style=f"bold {theme.SECONDARY}", border_style=theme.BORDER)
    table.add_column("Skill", style=f"bold {theme.PRIMARY}", no_wrap=True)
    table.add_column("Source", style=theme.TEXT_MUTED, no_wrap=True)
    table.add_column("Mode", style=theme.ACCENT, no_wrap=True)
    table.add_column("Description", style=theme.TEXT)
    for s in skills:
        name = getattr(s, "name", "")
        tag = f"/{name}" if getattr(s, "user_invocable", False) else name
        table.add_row(
            _safe_text(tag),
            _safe_text(getattr(s, "source", "")),
            _safe_text(getattr(s, "context", "")),
            _safe_text(getattr(s, "description", "")),
        )
    console.print(table)
