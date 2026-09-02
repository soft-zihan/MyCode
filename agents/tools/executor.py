"""ToolExecutor - 工具执行辅助函数。

提供工具执行相关的辅助函数：
- persist_large_result: 持久化大结果到文件
- detect_failure: 检测结果是否看起来像失败
"""

from __future__ import annotations

import time
from pathlib import Path


def persist_large_result(tool_name: str, result: str) -> str:
    """持久化大结果到文件。
    
    如果结果超过 30KB，保存到文件并返回预览。
    
    Args:
        tool_name: 工具名称
        result: 原始结果
    
    Returns:
        str: 处理后的结果（可能是预览或原结果）
    """
    THRESHOLD = 30 * 1024  # 30 KB
    if len(result.encode()) <= THRESHOLD:
        return result

    d = Path.home() / ".bear-code" / "tool-results"
    d.mkdir(parents=True, exist_ok=True)
    filename = f"{int(time.time() * 1000)}-{tool_name}.txt"
    filepath = d / filename
    filepath.write_text(result, encoding="utf-8")

    lines = result.split("\n")
    preview = "\n".join(lines[:200])
    size_kb = len(result.encode()) / 1024

    if tool_name == "read_file":
        return (
            f"[Result too large ({size_kb:.1f} KB, {len(lines)} lines). "
            f"Full output saved to {filepath}.]\n\n"
            f"Preview (first 200 lines):\n{preview}\n\n"
            f"To read more, use read_file with offset parameter:\n"
            f"  - offset=201 to read lines 201-400\n"
            f"  - offset=401 to read lines 401-600\n"
            f"  - Or use limit parameter to read specific ranges"
        )
    
    return (
        f"[Result too large ({size_kb:.1f} KB, {len(lines)} lines). "
        f"Full output saved to {filepath}. "
        f"You can use read_file to see the full result.]\n\n"
        f"Preview (first 200 lines):\n{preview}"
    )


def detect_failure(tool_name: str, raw: str, result: str) -> bool:
    """检测结果是否看起来像失败。
    
    Args:
        tool_name: 工具名称
        raw: 原始结果
        result: 处理后的结果
    
    Returns:
        bool: 是否看起来像失败
    """
    text = f"{raw}\n{result}".lower()
    if any(marker in text for marker in ("error", "denied", "timed out", "timeout")):
        return True
    if tool_name == "compact_context" and "no context compaction" in text:
        return True
    return False
