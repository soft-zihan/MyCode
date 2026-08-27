"""默认压缩策略实现。

封装现有的四级压缩管线逻辑，使其符合 CompactionStrategy 接口。
"""

from __future__ import annotations

import time
from typing import Any

from agents.seams.compaction import CompactionResult


class DefaultCompactionStrategy:
    """默认压缩策略（封装现有的四级压缩管线）。
    
    这是一个适配器，将现有的 Agent 压缩逻辑包装成 CompactionStrategy 接口。
    """
    
    def __init__(
        self,
        agent: Any,  # Agent 实例
        l1_threshold: float = 0.5,
        l2_threshold: float = 0.6,
        l3_threshold: float = 0.7,
        l4_threshold: float = 0.8,
    ) -> None:
        self._agent = agent
        self._l1_threshold = l1_threshold
        self._l2_threshold = l2_threshold
        self._l3_threshold = l3_threshold
        self._l4_threshold = l4_threshold
    
    def should_compact(
        self,
        context_tokens: int,
        context_window: int,
    ) -> bool:
        """判断是否需要压缩。"""
        if context_window <= 0:
            return False
        utilization = context_tokens / context_window
        return utilization > self._l1_threshold
    
    async def compact(
        self,
        messages: list[dict[str, Any]],
        *,
        system_prompt: str = "",
        side_query: Any = None,
        **kwargs: Any,
    ) -> CompactionResult:
        """执行压缩。
        
        注意：这是一个简化实现，实际的压缩逻辑在 Agent 类中。
        这里只提供接口框架，实际使用时需要调用 Agent 的压缩方法。
        """
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
    
    def run_compression_pipeline(self) -> None:
        """运行多级压缩管线。"""
        # 委托给 Agent 的现有方法
        if hasattr(self._agent, '_run_compression_pipeline'):
            self._agent._run_compression_pipeline()
