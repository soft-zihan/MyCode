"""Search Tools - 搜索工具。

包括：grep_search
"""

from __future__ import annotations

import fnmatch
import os
import re
from pathlib import Path


def grep_search(inp: dict) -> str:
    from agents.model.runtime import get_runtime, DockerRuntime
    rt = get_runtime()
    pattern = inp["pattern"]

    if isinstance(rt, DockerRuntime):
        path = inp.get("path") or "."
    else:
        path = str(_resolve_tool_path(inp.get("path") or "."))

    include = inp.get("include")

    result = rt.grep_search(pattern, path, include)
    if not result:
        return "No matches found."
    lines = [l for l in result.split("\n") if l]
    output = "\n".join(lines[:100])
    if len(lines) > 100:
        output += f"\n... and {len(lines) - 100} more matches"
    return output


def _resolve_tool_path(raw_path: str, *, must_exist: bool = True) -> Path:
    path = Path(raw_path)
    if path.exists() or not path.is_absolute():
        return path

    parts = path.parts
    cwd = Path.cwd()
    for i in range(1, len(parts)):
        candidate = cwd.joinpath(*parts[i:])
        if must_exist and candidate.exists():
            return candidate
        if not must_exist and candidate.parent.exists():
            return candidate

    return path


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
