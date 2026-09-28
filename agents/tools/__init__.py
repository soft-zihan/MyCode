"""Tools Package - 工具系统。

提供工具定义、执行、权限检查等功能。

工具分类：
- file_tools: 文件操作 (read_file, write_file, edit_file, list_files)
- outline_tools: 结构化预读 (outline_file)
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
     get_execution_mode,
     TOOL_EXECUTION_MODES,
     _truncate_result,
)

from agents.tools.file_tools import (
    read_file,
    write_file,
    edit_file,
    list_files,
)
from agents.tools.outline_tools import outline_file
from agents.tools.paths import resolve_tool_path

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
from agents.tools.web_tools import web_search

# Backward compatibility aliases
_grep_search = grep_search
_grep_python = grep_python
_web_search = web_search


from agents.tools.plan_tools import (
    plan_propose as _plan_propose_tool,
    plan_status as _plan_status_tool,
    plan_list as _plan_list_tool,
    plan_update as _plan_update_tool,
    plan_task_start as _plan_task_start_tool,
    plan_task_done as _plan_task_done_tool,
    plan_task_failed as _plan_task_failed_tool,
    plan_complete as _plan_complete_tool,
    plan_add_artifact as _plan_add_artifact_tool,
    plan_read_artifact as _plan_read_artifact_tool,
    plan_archive as _plan_archive_tool,
    plan_explore as _plan_explore_tool,
    plan_save_explore as _plan_save_explore_tool,
    plan_continue as _plan_continue_tool,
    plan_retry as _plan_retry_tool,
    plan_recall as _plan_recall_tool,
    plan_abandon as _plan_abandon_tool,
    plan_reopen as _plan_reopen_tool,
    plan_check_expired as _plan_check_expired_tool,
    plan_pause as _plan_pause_tool,
    plan_resume as _plan_resume_tool,
    plan_skip as _plan_skip_tool,
    plan_redo as _plan_redo_tool,
    plan_rollback as _plan_rollback_tool,
)

from agents.tools.permissions import (
    check_permission,
    is_dangerous,
    load_permission_rules,
    reset_permission_cache,
)
from agents.logging import print_info
from agents.skills.skills import create_skill
from agents.tools.runtime import get_runtime, DockerRuntime


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
            rt = get_runtime()
            if isinstance(rt, DockerRuntime):
                abs_path = inp["file_path"]
                if not abs_path.startswith("/"):
                    abs_path = f"{rt.workdir}/{abs_path}"
                read_file_state[abs_path] = 0
            else:
                abs_path = str(resolve_tool_path(inp["file_path"]).resolve())
                try:
                    read_file_state[abs_path] = os.path.getmtime(abs_path)
                except OSError:
                    pass
        return _truncate_result(result)

    if name in ("write_file", "edit_file") and read_file_state is not None:
        rt = get_runtime()
        if isinstance(rt, DockerRuntime):
            abs_path = inp["file_path"]
            if not abs_path.startswith("/"):
                abs_path = f"{rt.workdir}/{abs_path}"
        else:
            abs_path = str(resolve_tool_path(inp["file_path"], must_exist=(name == "edit_file")).resolve())
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

    if name == "skill_create":
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
        "outline_file": outline_file,
        "write_file": write_file,
        "edit_file": edit_file,
        "list_files": list_files,
        "grep_search": grep_search,
        "web_search": web_search,
        "run_shell": run_shell,
        "shell_status": shell_status,
        "plan_propose": _plan_propose_tool,
        "plan_status": _plan_status_tool,
        "plan_list": _plan_list_tool,
        "plan_update": _plan_update_tool,
        "plan_task_start": _plan_task_start_tool,
        "plan_task_done": _plan_task_done_tool,
        "plan_task_failed": _plan_task_failed_tool,
        "plan_complete": _plan_complete_tool,
        "plan_add_artifact": _plan_add_artifact_tool,
        "plan_read_artifact": _plan_read_artifact_tool,
        "plan_archive": _plan_archive_tool,
        "plan_explore": _plan_explore_tool,
        "plan_save_explore": _plan_save_explore_tool,
        "plan_continue": _plan_continue_tool,
        "plan_retry": _plan_retry_tool,
        "plan_recall": _plan_recall_tool,
        "plan_abandon": _plan_abandon_tool,
        "plan_reopen": _plan_reopen_tool,
        "plan_check_expired": _plan_check_expired_tool,
        "plan_pause": _plan_pause_tool,
        "plan_resume": _plan_resume_tool,
        "plan_skip": _plan_skip_tool,
        "plan_redo": _plan_redo_tool,
        "plan_rollback": _plan_rollback_tool,
    }
    handler = handlers.get(name)

    if not handler:
        return f"Unknown tool: {name}"

    import inspect
    import time
    t0 = time.time()
    if inspect.iscoroutinefunction(handler):
        print_info(f"[DEBUG] execute_tool: calling async handler for {name}")
        result = await handler(inp)
    else:
        print_info(f"[DEBUG] execute_tool: calling asyncio.to_thread for {name}")
        result = await asyncio.to_thread(handler, inp)
    print_info(f"[DEBUG] execute_tool: handler done for {name}, took {time.time()-t0:.2f}s")
    result = _truncate_result(result)

    if name in ("write_file", "edit_file") and read_file_state is not None and not result.startswith("Error"):
        abs_path = str(resolve_tool_path(inp["file_path"], must_exist=False).resolve())
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
    "get_execution_mode",
    "TOOL_EXECUTION_MODES",
    "execute_tool",
    "check_permission",
    "is_dangerous",
    "load_permission_rules",
    "reset_permission_cache",
    "set_background_done_callback",
    "outline_file",
    # Backward compatibility
    "read_file",
    "write_file",
    "edit_file",
    "list_files",
    "run_shell",
    "shell_status",
    "grep_search",
    "grep_python",
    "web_search",
    "_read_file",
    "_write_file",
    "_edit_file",
    "_list_files",
    "_run_shell",
    "_shell_status",
    "_grep_search",
    "_grep_python",
    "_web_search",
    "resolve_tool_path",
]
