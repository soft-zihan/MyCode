"""ToolExecutor - 工具执行辅助函数。

提供工具执行相关的辅助函数：
- persist_large_result: 持久化大结果到文件
- detect_failure: 检测结果是否看起来像失败
"""

from __future__ import annotations

import re
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

    d = Path.home() / ".my-code" / "tool-results"
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


# 结构化载荷里「明确为空」的 error 类字段。摘掉它再扫子串，因为键名本身不是失败信号：
# task_list 的每个任务都带 `error` 字段（失败原因，成功时为空串），于是 `"error": ""`
# 里的子串会让**每次成功的清单更新**都被判成失败——outcome 落成 error、
# `_tool_error_streak` 一路推高，而它是写进提示词的（prompt_runtime 的
# "Recent tool error streak"），模型会在一场完全正常的执行里被告知「你最近连续失败」。
# 只摘**空值**、不摘键名本身：`{"error": "boom"}` 与 `"status": "error"` 仍然会被抓到，
# 所以这条不引入新的假阴性。
_EMPTY_ERROR_FIELD_RE = re.compile(
    r"""["'](error|denied|timeout|timed_out)["']\s*:\s*("""
    r"""""|''|null|None|false|False|\[\]|\{\})""",
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
    text = _EMPTY_ERROR_FIELD_RE.sub("", f"{raw}\n{result}").lower()
    if any(marker in text for marker in ("error", "denied", "timed out", "timeout")):
        return True
    if tool_name == "compact_context" and "no context compaction" in text:
        return True
    return False
