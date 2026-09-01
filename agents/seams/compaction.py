"""Compaction Seam 接口定义。

压缩策略抽象。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


# ============================================================
# 数据类型
# ============================================================


@dataclass
class CompactionResult:
    """压缩结果。"""
    
    summary: str = ""
    retained_tail: list[dict[str, Any]] = field(default_factory=list)
    removed_count: int = 0
    details: dict[str, Any] = field(default_factory=dict)
    read_files: list[str] = field(default_factory=list)
    modified_files: list[str] = field(default_factory=list)


# ============================================================
# CompactionStrategy 接口
# ============================================================


class CompactionStrategy(Protocol):
    """压缩策略协议。"""
    
    def should_compact(self, context_tokens: int, context_window: int) -> bool: ...
    def compact(
        self,
        messages: list[dict[str, Any]],
        *,
        system_prompt: str = "",
        side_query: Any = None,
        **kwargs: Any,
    ) -> CompactionResult: ...
