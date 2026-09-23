"""UTF-8 清洗工具（无依赖叶子模块）。

模型/工具输出可能包含孤立代理对等非法 UTF-16 序列，json.dumps 落盘或
API 请求会抛 UnicodeEncodeError——统一在入库/发请求前 replace 清洗。
原 agent_loop / context_compressor 各有一份逐字重复实现，U0 收敛于此。
"""

from __future__ import annotations

from typing import Any


def safe_utf8_text(text: str) -> str:
    if not text:
        return text
    try:
        text.encode("utf-8")
        return text
    except UnicodeEncodeError:
        return text.encode("utf-8", errors="replace").decode("utf-8")


def sanitize_for_utf8(obj: Any) -> Any:
    if isinstance(obj, str):
        return safe_utf8_text(obj)
    if isinstance(obj, list):
        return [sanitize_for_utf8(x) for x in obj]
    if isinstance(obj, tuple):
        return tuple(sanitize_for_utf8(x) for x in obj)
    if isinstance(obj, dict):
        return {sanitize_for_utf8(k): sanitize_for_utf8(v) for k, v in obj.items()}
    return obj
