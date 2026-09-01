#!/usr/bin/env python3
"""Tools module - 向后兼容包装器。

所有实际实现已迁移到 agents/tools/ 包中。
此文件保留用于向后兼容。
"""

from __future__ import annotations

from agents.tools import (
    ToolDef,
    PermissionMode,
    READ_TOOLS,
    EDIT_TOOLS,
    CONCURRENCY_SAFE_TOOLS,
    MAX_RESULT_CHARS,
    tool_definitions,
    get_active_tool_definitions,
    get_deferred_tool_names,
    reset_activated_tools,
    activate_tool,
    get_execution_mode,
    TOOL_EXECUTION_MODES,
    execute_tool,
    check_permission,
    is_dangerous,
    load_permission_rules,
    reset_permission_cache,
    set_background_done_callback,
)

__all__ = [
    "ToolDef",
    "PermissionMode",
    "READ_TOOLS",
    "EDIT_TOOLS",
    "CONCURRENCY_SAFE_TOOLS",
    "MAX_RESULT_CHARS",
    "tool_definitions",
    "get_active_tool_definitions",
    "get_deferred_tool_names",
    "reset_activated_tools",
    "activate_tool",
    "get_execution_mode",
    "TOOL_EXECUTION_MODES",
    "execute_tool",
    "check_permission",
    "is_dangerous",
    "load_permission_rules",
    "reset_permission_cache",
    "set_background_done_callback",
]
