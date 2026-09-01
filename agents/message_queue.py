"""MessageQueue - 双队列消息系统。

支持两种消息注入方式：
- steering: 中途注入，当前轮工具调用完成后生效
- follow_up: 结束后注入，Agent 停止前检查

与 Hook 系统集成，实现事件驱动的消息注入。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field


@dataclass
class QueuedMessage:
    """队列中的消息。
    
    Attributes:
        content: 消息内容
        source: 消息来源 ("user" / "hook" / "system")
        injected_at: 注入时间（ISO 格式）
    """
    
    content: str
    source: str = "user"
    injected_at: str = ""
    
    def __post_init__(self) -> None:
        if not self.injected_at:
            self.injected_at = time.strftime("%Y-%m-%dT%H:%M:%S")


class MessageQueue:
    """双队列消息系统。
    
    - steering 队列：中途注入，当前轮工具调用完成后生效
    - follow_up 队列：结束后注入，Agent 停止前检查
    
    线程安全：使用 asyncio.Lock 保护队列操作。
    """
    
    def __init__(self) -> None:
        self._steering: list[QueuedMessage] = []
        self._follow_up: list[QueuedMessage] = []
        self._lock = asyncio.Lock()
    
    async def steer(self, message: str, source: str = "user") -> None:
        """中途注入：当前轮工具调用完成后生效。
        
        Args:
            message: 消息内容
            source: 消息来源
        """
        async with self._lock:
            self._steering.append(QueuedMessage(
                content=message,
                source=source,
            ))
    
    async def follow_up(self, message: str, source: str = "user") -> None:
        """结束后注入：Agent 停止前检查。
        
        Args:
            message: 消息内容
            source: 消息来源
        """
        async with self._lock:
            self._follow_up.append(QueuedMessage(
                content=message,
                source=source,
            ))
    
    async def drain_steering(self) -> list[QueuedMessage]:
        """排空 steering 队列，返回所有消息。"""
        async with self._lock:
            msgs = self._steering.copy()
            self._steering.clear()
            return msgs
    
    async def drain_follow_up(self) -> list[QueuedMessage]:
        """排空 follow_up 队列，返回所有消息。"""
        async with self._lock:
            msgs = self._follow_up.copy()
            self._follow_up.clear()
            return msgs
    
    async def has_steering(self) -> bool:
        """检查 steering 队列是否有消息。"""
        async with self._lock:
            return len(self._steering) > 0
    
    async def has_follow_up(self) -> bool:
        """检查 follow_up 队列是否有消息。"""
        async with self._lock:
            return len(self._follow_up) > 0
    
    async def clear(self) -> None:
        """清空所有队列。"""
        async with self._lock:
            self._steering.clear()
            self._follow_up.clear()
    
    @property
    def steering_count(self) -> int:
        """steering 队列消息数（非线程安全，仅用于调试）。"""
        return len(self._steering)
    
    @property
    def follow_up_count(self) -> int:
        """follow_up 队列消息数（非线程安全，仅用于调试）。"""
        return len(self._follow_up)
