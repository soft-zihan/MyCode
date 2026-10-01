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

import asyncio
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


from agents.tools.permissions import (
    check_permission,
    is_dangerous,
    load_permission_rules,
    reset_permission_cache,
)
from agents.logging import print_info
from agents.skills.skills import create_skill
from agents.tools.runtime import get_runtime, DockerRuntime


_TOOL_HANDLERS: dict[str, Any] = {
    "outline_file": outline_file,
    "write_file": write_file,
    "edit_file": edit_file,
    "list_files": list_files,
    "grep_search": grep_search,
    "web_search": web_search,
    "run_shell": run_shell,
    "shell_status": shell_status,
}


def _resolve_abs_tool_path(inp: dict, must_exist: bool = False) -> str:
    """工具路径解析为绝对路径（docker 下拼 runtime workdir，不落宿主磁盘）。"""
    rt = get_runtime()
    if isinstance(rt, DockerRuntime):
        abs_path = inp["file_path"]
        if not abs_path.startswith("/"):
            abs_path = f"{rt.workdir}/{abs_path}"
        return abs_path
    return str(resolve_tool_path(inp["file_path"], must_exist=must_exist).resolve())


def _check_write_freshness(
    name: str, inp: dict, read_file_state: dict[str, float]
) -> str | None:
    """写前保护：未读过/读后被外部修改的文件拒绝写入；通过返回 None。"""
    rt = get_runtime()
    abs_path = _resolve_abs_tool_path(inp, must_exist=(name == "edit_file"))
    if isinstance(rt, DockerRuntime) or not os.path.exists(abs_path):
        return None
    # 允许写入空文件（刚创建的文件）
    file_size = os.path.getsize(abs_path)
    if file_size > 0:
        if abs_path not in read_file_state:
            verb = "writing" if name == "write_file" else "editing"
            return f"Error: You must read this file before {verb}. Use read_file first to see its current contents."
        if os.path.getmtime(abs_path) != read_file_state[abs_path]:
            verb = "writing" if name == "write_file" else "editing"
            return f"Warning: {inp['file_path']} was modified externally since your last read. Please read_file again before {verb}."
    return None


def _execute_skill_create(inp: dict) -> str:
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


async def _call_tool_handler(name: str, handler: Any, inp: dict) -> str:
    """调用 handler：async 直接 await，sync 走线程池；计时并截断结果。"""
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
    return _truncate_result(result)


async def execute_tool(
    name: str,
    inp: dict,
    read_file_state: dict[str, float] | None = None,
) -> str:
    """执行工具调用。"""
    if name == "read_file":
        result = read_file(inp)
        if read_file_state is not None and not result.startswith("Error"):
            # 记录读取时 mtime（docker 下无宿主 mtime，记 0 表示"已读"）
            abs_path = _resolve_abs_tool_path(inp)
            if isinstance(get_runtime(), DockerRuntime):
                read_file_state[abs_path] = 0
            else:
                try:
                    read_file_state[abs_path] = os.path.getmtime(abs_path)
                except OSError:
                    pass
        return _truncate_result(result)

    if name in ("write_file", "edit_file") and read_file_state is not None:
        freshness_error = _check_write_freshness(name, inp, read_file_state)
        if freshness_error:
            return freshness_error

    if name == "skill_create":
        return _execute_skill_create(inp)

    handler = _TOOL_HANDLERS.get(name)
    if not handler:
        return f"Unknown tool: {name}"

    result = await _call_tool_handler(name, handler, inp)

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
