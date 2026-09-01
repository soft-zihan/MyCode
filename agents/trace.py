"""agents.trace 向后兼容 shim — 所有实际逻辑已移至 agents.observability.trace"""

from agents.observability.trace import (
    format_recent_events,
    get_trace_session,
    recent_events,
    set_trace_enabled,
    set_trace_session,
    trace_dir,
    trace_enabled,
    trace_event,
    trace_path,
    trace_tool_input,
)

__all__ = [
    "format_recent_events",
    "get_trace_session",
    "recent_events",
    "set_trace_enabled",
    "set_trace_session",
    "trace_dir",
    "trace_enabled",
    "trace_event",
    "trace_path",
    "trace_tool_input",
]
