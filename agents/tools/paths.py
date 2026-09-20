"""Path Utilities - 路径解析工具。"""

from __future__ import annotations

from pathlib import Path

from agents.core.workspace import get_workspace


def _absolute(path: Path | str) -> Path:
    return Path(str(path)).expanduser().resolve(strict=False)


def resolve_tool_path(raw_path: str | Path, *, must_exist: bool = True) -> Path:
    """把工具路径锚定到当前会话工作区。"""
    workspace = _absolute(get_workspace())
    path = Path(str(raw_path or "")).expanduser()

    if not path.is_absolute():
        return _absolute(workspace / path)

    absolute = _absolute(path)
    if absolute.exists() or not must_exist:
        return absolute

    parts = absolute.parts
    for i in range(1, len(parts)):
        candidate = _absolute(workspace.joinpath(*parts[i:]))
        if candidate.exists():
            return candidate

    return absolute


def workspace_guard_error(path: Path | str) -> str | None:
    """递归搜索/列目录工具的工作区边界守卫。"""
    workspace = _absolute(get_workspace())
    target = _absolute(path)
    if target == workspace or workspace in target.parents:
        return None
    return f"Error: path '{path}' is outside the workspace"
