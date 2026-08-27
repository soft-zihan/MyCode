"""Compaction Seam 接口定义。

压缩策略抽象，支持多种实现：
- FourLevelCompaction：四级可逆压缩管线（BearCode 特色）
- SingleLevelCompaction：单级压缩（Pi 的方式）
- CustomCompaction：自定义压缩策略
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
    # 累计文件追踪
    read_files: list[str] = field(default_factory=list)
    modified_files: list[str] = field(default_factory=list)


# ============================================================
# CompactionStrategy 接口
# ============================================================


class CompactionStrategy(Protocol):
    """压缩策略协议。
    
    所有压缩策略实现必须遵守此接口。
    """
    
    def should_compact(
        self,
        context_tokens: int,
        context_window: int,
    ) -> bool:
        """判断是否需要压缩。
        
        Args:
            context_tokens: 当前上下文 token 数
            context_window: 上下文窗口大小
        
        Returns:
            是否需要压缩
        """
        ...
    
    def compact(
        self,
        messages: list[dict[str, Any]],
        *,
        system_prompt: str = "",
        side_query: Any = None,
        **kwargs: Any,
    ) -> CompactionResult:
        """执行压缩。
        
        Args:
            messages: 当前消息列表
            system_prompt: 系统提示词
            side_query: 用于生成摘要的 side query 函数
            **kwargs: 其他参数
        
        Returns:
            压缩结果
        """
        ...


# ============================================================
# 默认实现：四级可逆压缩管线
# ============================================================


class FourLevelCompaction:
    """四级可逆压缩管线（BearCode 特色）。
    
    L1: 预算截断（利用率 >50%）
    L2: Snip（利用率 >60%，原文存入 ContextStore，可恢复）
    L3: Microcompact（利用率 >70%，原文存入 ContextStore，可恢复）
    L4: 语义 compact（利用率 >80%，LLM 折叠，不可逆）
    """
    
    def __init__(
        self,
        context_store: Any = None,
        l1_threshold: float = 0.5,
        l2_threshold: float = 0.6,
        l3_threshold: float = 0.7,
        l4_threshold: float = 0.8,
    ) -> None:
        self._context_store = context_store
        self._l1_threshold = l1_threshold
        self._l2_threshold = l2_threshold
        self._l3_threshold = l3_threshold
        self._l4_threshold = l4_threshold
    
    def should_compact(
        self,
        context_tokens: int,
        context_window: int,
    ) -> bool:
        if context_window <= 0:
            return False
        utilization = context_tokens / context_window
        return utilization > self._l1_threshold
    
    def compact(
        self,
        messages: list[dict[str, Any]],
        *,
        system_prompt: str = "",
        side_query: Any = None,
        **kwargs: Any,
    ) -> CompactionResult:
        """执行四级压缩。
        
        注意：这是一个简化实现，实际的四级压缩逻辑在 agent.py 中。
        这里只提供接口框架，实际使用时需要注入完整的压缩逻辑。
        """
        # 提取文件操作
        from agents.session_memory import extract_file_ops
        read_files, modified_files = extract_file_ops(messages)
        
        # 简化：直接返回原消息（实际实现会执行四级压缩）
        return CompactionResult(
            summary="",
            retained_tail=list(messages),
            removed_count=0,
            details={},
            read_files=read_files,
            modified_files=modified_files,
        )


# ============================================================
# 单级压缩实现
# ============================================================


class SingleLevelCompaction:
    """单级压缩（Pi 的方式）。
    
    只有一个阈值（默认 80%），直接 LLM 折叠，不可逆。
    """
    
    def __init__(
        self,
        threshold: float = 0.8,
        keep_recent_tokens: int = 8000,
    ) -> None:
        self._threshold = threshold
        self._keep_recent_tokens = keep_recent_tokens
    
    def should_compact(
        self,
        context_tokens: int,
        context_window: int,
    ) -> bool:
        if context_window <= 0:
            return False
        utilization = context_tokens / context_window
        return utilization > self._threshold
    
    def compact(
        self,
        messages: list[dict[str, Any]],
        *,
        system_prompt: str = "",
        side_query: Any = None,
        **kwargs: Any,
    ) -> CompactionResult:
        """执行单级压缩。"""
        # 提取文件操作
        from agents.session_memory import extract_file_ops
        read_files, modified_files = extract_file_ops(messages)
        
        # 简化：直接返回原消息
        return CompactionResult(
            summary="",
            retained_tail=list(messages),
            removed_count=0,
            details={},
            read_files=read_files,
            modified_files=modified_files,
        )
