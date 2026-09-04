"""Tools Package - 工具系统。

提供工具定义、执行、权限检查等功能。

工具分类：
- file_tools: 文件操作 (read_file, write_file, edit_file, list_files)
- shell_tools: Shell 命令 (run_shell, shell_status)
- search_tools: 搜索 (grep_search)
- permissions: 权限检查
- registry: 工具注册表和执行模式
"""

from __future__ import annotations

import json
import os
from typing import Any

from agents.tools.registry import (
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
    _truncate_result,
    _activated_tools,
)

from agents.tools.file_tools import (
    read_file,
    write_file,
    edit_file,
    list_files,
    _resolve_tool_path,
)

# Backward compatibility aliases
_read_file = read_file
_write_file = write_file
_edit_file = edit_file
_list_files = list_files

from agents.tools.shell_tools import (
    run_shell,
    shell_status,
    set_background_done_callback,
    _start_background_shell,
    BACKGROUND_JOBS,
)

# Backward compatibility aliases
_run_shell = run_shell
_shell_status = shell_status
_BACKGROUND_JOBS = BACKGROUND_JOBS

from agents.tools.search_tools import (
    grep_search,
    grep_python,
)

# Backward compatibility aliases
_grep_search = grep_search
_grep_python = grep_python

from agents.tools.permissions import (
    check_permission,
    is_dangerous,
    load_permission_rules,
    reset_permission_cache,
)


async def execute_tool(
    name: str,
    inp: dict,
    read_file_state: dict[str, float] | None = None,
) -> str:
    """执行工具调用。"""
    import asyncio

    if name == "read_file":
        result = read_file(inp)
        if read_file_state is not None and not result.startswith("Error"):
            from agents.tools.runtime import get_runtime, DockerRuntime
            rt = get_runtime()
            if isinstance(rt, DockerRuntime):
                abs_path = inp["file_path"]
                if not abs_path.startswith("/"):
                    abs_path = f"{rt.workdir}/{abs_path}"
                read_file_state[abs_path] = 0
            else:
                abs_path = str(_resolve_tool_path(inp["file_path"]).resolve())
                try:
                    read_file_state[abs_path] = os.path.getmtime(abs_path)
                except OSError:
                    pass
        return _truncate_result(result)

    if name in ("write_file", "edit_file") and read_file_state is not None:
        from agents.tools.runtime import get_runtime, DockerRuntime
        rt = get_runtime()
        if isinstance(rt, DockerRuntime):
            abs_path = inp["file_path"]
            if not abs_path.startswith("/"):
                abs_path = f"{rt.workdir}/{abs_path}"
        else:
            abs_path = str(_resolve_tool_path(inp["file_path"], must_exist=(name == "edit_file")).resolve())
        if not isinstance(rt, DockerRuntime) and os.path.exists(abs_path):
            # 允许写入空文件（刚创建的文件）
            file_size = os.path.getsize(abs_path)
            if file_size > 0:
                if abs_path not in read_file_state:
                    verb = "writing" if name == "write_file" else "editing"
                    return f"Error: You must read this file before {verb}. Use read_file first to see its current contents."
                if os.path.getmtime(abs_path) != read_file_state[abs_path]:
                    verb = "writing" if name == "write_file" else "editing"
                    return f"Warning: {inp['file_path']} was modified externally since your last read. Please read_file again before {verb}."

    if name == "tool_search":
        query = (inp.get("query") or "").lower()
        deferred = [t for t in tool_definitions if t.get("deferred")]
        matches = [
            t for t in deferred
            if query in t["name"].lower() or query in (t.get("description") or "").lower()
        ]
        if not matches:
            return "No matching deferred tools found."

        for m in matches:
            activate_tool(m["name"])

        return json.dumps(
            [{"name": t["name"], "description": t.get("description", ""), "input_schema": t["input_schema"]} for t in matches],
            indent=2,
        )

    if name == "memory":
        from agents.memory.memory import memory_tool
        result = memory_tool(
            action=inp.get("action", ""),
            name=inp.get("name", ""),
            type=inp.get("type", ""),
            description=inp.get("description", ""),
            content=inp.get("content", ""),
            match=inp.get("match", ""),
        )
        return _truncate_result(json.dumps(result, ensure_ascii=False, indent=2))

    if name == "skill_evolve":
        from agents.skills.skills import evolve_skill
        result = evolve_skill(
            skill_name=inp.get("skill_name", ""),
            lesson=inp.get("lesson", ""),
            rationale=inp.get("rationale", ""),
            target=inp.get("target", "active"),
        )
        return _truncate_result(json.dumps(result, ensure_ascii=False, indent=2))

    if name == "skill_create":
        from agents.skills.skills import create_skill
        result = create_skill(
            name=inp.get("name", ""),
            description=inp.get("description", ""),
            instructions=inp.get("instructions", ""),
            when_to_use=inp.get("when_to_use") or inp.get("when-to-use", ""),
            target=inp.get("target", "project"),
            context=inp.get("context", "inline"),
            user_invocable=bool(inp.get("user_invocable", False)),
            allowed_tools=inp.get("allowed_tools"),
            evidence=inp.get("evidence", ""),
        )
        return _truncate_result(json.dumps(result, ensure_ascii=False, indent=2))

    handlers: dict = {
        "write_file": write_file,
        "edit_file": edit_file,
        "list_files": list_files,
        "grep_search": grep_search,
        "run_shell": run_shell,
        "shell_status": shell_status,
    }
    handler = handlers.get(name)

    if not handler:
        return f"Unknown tool: {name}"

    # Run blocking handlers in thread pool to avoid blocking event loop
    result = await asyncio.to_thread(handler, inp)
    result = _truncate_result(result)

    if name in ("write_file", "edit_file") and read_file_state is not None and not result.startswith("Error"):
        abs_path = str(_resolve_tool_path(inp["file_path"], must_exist=False).resolve())
        try:
            read_file_state[abs_path] = os.path.getmtime(abs_path)
        except OSError:
            pass

    return result


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
    # Backward compatibility
    "read_file",
    "write_file",
    "edit_file",
    "list_files",
    "run_shell",
    "shell_status",
    "grep_search",
    "grep_python",
    "_read_file",
    "_write_file",
    "_edit_file",
    "_list_files",
    "_run_shell",
    "_shell_status",
    "_grep_search",
    "_grep_python",
    "_resolve_tool_path",
]
