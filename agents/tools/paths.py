"""Path Utilities - 路径解析工具。

提供工具路径解析和工作区守卫功能。
"""

from __future__ import annotations

from pathlib import Path


def resolve_tool_path(raw_path: str, *, must_exist: bool = True) -> Path:
    """解析工具路径。
    
    如果路径存在或不是绝对路径，直接返回。
    否则尝试从当前工作区解析。
    
    Args:
        raw_path: 原始路径字符串
        must_exist: 是否要求路径必须存在
    
    Returns:
        解析后的 Path 对象
    """
    path = Path(raw_path)
    if path.exists() or not path.is_absolute():
        return path

    parts = path.parts
    # 使用工作区而不是 CWD
    from agents.core.workspace import get_workspace
    workspace = get_workspace()
    for i in range(1, len(parts)):
        candidate = workspace.joinpath(*parts[i:])
        if must_exist and candidate.exists():
            return candidate
        if not must_exist and candidate.parent.exists():
            return candidate

    return path


def workspace_guard_error(path: str) -> str:
    """工作区守卫错误消息。
    
    Args:
        path: 尝试访问的路径
    
    Returns:
        错误消息字符串
    """
    return f"Error: path '{path}' is outside workspace"
