"""File Tools - 文件操作工具。

包括：read_file, write_file, edit_file, list_files
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any


from agents.tools.paths import resolve_tool_path, workspace_guard_error


def read_file(inp: dict) -> str:
    try:
        from agents.tools.runtime import get_runtime, DockerRuntime
        rt = get_runtime()

        if isinstance(rt, DockerRuntime):
            path_str = inp["file_path"]
        else:
            path_str = str(resolve_tool_path(inp["file_path"]))

        content = rt.read_file(path_str)
        lines = content.split("\n")

        offset = inp.get("offset", 1) - 1
        limit = inp.get("limit")

        if offset > 0 or limit is not None:
            if limit is not None:
                lines = lines[offset:offset + limit]
            else:
                lines = lines[offset:]
            start_line = offset + 1
        else:
            start_line = 1

        numbered = "\n".join(f"{start_line + i:4d} | {line}" for i, line in enumerate(lines))

        if offset > 0 or limit is not None:
            total_lines = len(content.split("\n"))
            shown_end = start_line + len(lines) - 1
            if shown_end < total_lines:
                numbered += f"\n\n... ({total_lines - shown_end} more lines, use offset={shown_end + 1} to continue)"

        return numbered
    except Exception as e:
        return f"Error reading file: {e}"


def write_file(inp: dict) -> str:
    try:
        from agents.tools.runtime import get_runtime, DockerRuntime
        rt = get_runtime()

        if isinstance(rt, DockerRuntime):
            path_str = inp["file_path"]
        else:
            path_str = str(resolve_tool_path(inp["file_path"], must_exist=False))

        rt.write_file(path_str, inp["content"])
        lines = inp["content"].split("\n")
        line_count = len(lines)
        preview = "\n".join(f"{i + 1:4d} | {l}" for i, l in enumerate(lines[:30]))
        trunc = f"\n  ... ({line_count} lines total)" if line_count > 30 else ""
        return f"Successfully wrote to {inp['file_path']} ({line_count} lines)\n\n{preview}{trunc}"
    except Exception as e:
        return f"Error writing file: {e}"



def _normalize_quotes(s: str) -> str:
    s = re.sub("[\u2018\u2019\u2032]", "'", s)
    s = re.sub('[\u201c\u201d\u2033]', '"', s)
    return s


def _find_actual_string(file_content: str, search_string: str) -> str | None:
    if search_string in file_content:
        return search_string
    norm_search = _normalize_quotes(search_string)
    norm_file = _normalize_quotes(file_content)
    idx = norm_file.find(norm_search)
    if idx != -1:
        return file_content[idx:idx + len(search_string)]
    return None


def _generate_diff(old_content: str, old_string: str, new_string: str) -> str:
    before_change = old_content.split(old_string)[0]
    line_num = before_change.count("\n") + 1
    old_lines = old_string.split("\n")
    new_lines = new_string.split("\n")

    parts = [f"@@ -{line_num},{len(old_lines)} +{line_num},{len(new_lines)} @@"]
    for l in old_lines:
        parts.append(f"- {l}")
    for l in new_lines:
        parts.append(f"+ {l}")
    return "\n".join(parts)


def edit_file(inp: dict) -> str:
    try:
        from agents.tools.runtime import get_runtime, DockerRuntime
        rt = get_runtime()

        if isinstance(rt, DockerRuntime):
            path_str = inp["file_path"]
        else:
            path_str = str(resolve_tool_path(inp["file_path"]))

        content = rt.read_file(path_str)

        actual = _find_actual_string(content, inp["old_string"])
        if not actual:
            return f"Error: old_string not found in {inp['file_path']}"

        occurrences = content.count(inp["old_string"])
        if occurrences > 1:
            return f"Error: old_string found {occurrences} times in {inp['file_path']}. Must be unique."

        new_content = content.replace(actual, inp["new_string"], 1)
        rt.write_file(path_str, new_content)

        diff = _generate_diff(content, actual, inp["new_string"])

        quote_note = " (matched via quote normalization)" if actual != inp["old_string"] else ""

        return f"Successfully edited {inp['file_path']}{quote_note}\n\n{diff}"
    except Exception as e:
        return f"Error editing file: {e}"


def list_files(inp: dict) -> str:
    try:
        from agents.tools.runtime import get_runtime, DockerRuntime
        rt = get_runtime()

        if isinstance(rt, DockerRuntime):
            base = inp.get("path") or "."
        else:
            resolved = resolve_tool_path(inp.get("path") or ".")
            guard_error = workspace_guard_error(resolved)
            if guard_error:
                return guard_error
            base = str(resolved)

        pattern = inp["pattern"]
        files = rt.list_files(base, pattern)
        if not files:
            return "No files found matching the pattern."
        result = "\n".join(files[:200])
        if len(files) > 200:
            result += f"\n...and {len(files) - 200} more files are found ..."
        return result
    except Exception as e:
        return f"Error listing files: {e}"
