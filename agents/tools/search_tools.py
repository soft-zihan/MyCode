"""Search Tools - 搜索工具。

包括：grep_search
"""

from __future__ import annotations

import fnmatch
import os
import re
from pathlib import Path

from agents.tools.paths import resolve_tool_path, workspace_guard_error
from agents.tools.runtime import get_runtime, DockerRuntime


def grep_search(inp: dict) -> str:
    rt = get_runtime()
    pattern = inp["pattern"]

    if isinstance(rt, DockerRuntime):
        path = inp.get("path") or "."
    else:
        resolved = resolve_tool_path(inp.get("path") or ".")
        guard_error = workspace_guard_error(resolved)
        if guard_error:
            return guard_error
        path = str(resolved)

    include = inp.get("include")

    result = rt.grep_search(pattern, path, include)
    if not result:
        return "No matches found."
    lines = [l for l in result.split("\n") if l]
    output = "\n".join(lines[:100])
    if len(lines) > 100:
        output += f"\n... and {len(lines) - 100} more matches"
    return output


def grep_python(pattern: str, directory: str, include: str | None) -> str:
    regex = re.compile(pattern)
    include_pattern = include
    matches: list[str] = []

    def walk(d: str) -> None:
        if len(matches) >= 200:
            return
        try:
            entries = os.listdir(d)
        except Exception:
            return
        for name in entries:
            if name.startswith(".") or name == "node_modules":
                continue
            full = os.path.join(d, name)
            if os.path.isdir(full):
                walk(full)
                continue
            if include_pattern and not fnmatch.fnmatch(name, include_pattern):
                continue
            try:
                text = Path(full).read_text(errors="replace")
                for i, line in enumerate(text.split("\n")):
                    if regex.search(line):
                        matches.append(f"{full}:{i+1}:{line}")
                        if len(matches) >= 200:
                            return
            except Exception:
                pass

    walk(directory)
    if not matches:
        return "No matches found."
    output = "\n".join(matches[:100])
    if len(matches) > 100:
        output += f"\n... and {len(matches) - 100} more matches"
    return output
