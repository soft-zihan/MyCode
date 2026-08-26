"""Event bus for extension system.

支持 20+ 生命周期事件，部分事件可拦截或修改。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Awaitable


class EventType(str, Enum):
    """事件类型。"""
    # 会话事件
    SESSION_START = "session_start"
    SESSION_END = "session_end"

    # 输入事件（可拦截）
    INPUT = "input"

    # Agent 循环事件
    BEFORE_AGENT_START = "before_agent_start"
    AGENT_START = "agent_start"
    AGENT_END = "agent_end"

    # 轮次事件
    TURN_START = "turn_start"
    TURN_END = "turn_end"

    # 工具事件（可拦截/修改）
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"

    # Provider 事件
    BEFORE_PROVIDER_REQUEST = "before_provider_request"
    AFTER_PROVIDER_RESPONSE = "after_provider_response"

    # 压缩事件
    BEFORE_COMPACT = "before_compact"
    AFTER_COMPACT = "after_compact"

    # 进化事件
    BEFORE_SKILL_EVOLUTION = "before_skill_evolution"
    AFTER_SKILL_EVOLUTION = "after_skill_evolution"

    # Memory 事件
    MEMORY_RECALL = "memory_recall"

    # 模型事件
    MODEL_SELECT = "model_select"


# 可拦截的事件（handler 可返回 block=True 阻止执行）
INTERCEPTABLE_EVENTS = {
    EventType.INPUT,
    EventType.TOOL_CALL,
    EventType.BEFORE_COMPACT,
    EventType.BEFORE_SKILL_EVOLUTION,
}

# 可修改的事件（handler 可返回 modified 数据）
MODIFIABLE_EVENTS = {
    EventType.TOOL_RESULT,
    EventType.BEFORE_PROVIDER_REQUEST,
    EventType.MEMORY_RECALL,
}


@dataclass
class Event:
    """事件数据。"""
    type: EventType
    data: dict[str, Any] = field(default_factory=dict)
    # 拦截相关
    blocked: bool = False
    block_reason: str = ""
    # 修改相关
    modified: bool = False
    modified_data: Any = None

    def block(self, reason: str = "") -> None:
        """拦截事件。"""
        self.blocked = True
        self.block_reason = reason

    def modify(self, data: Any) -> None:
        """修改事件数据。"""
        self.modified = True
        self.modified_data = data


@dataclass
class EventResult:
    """事件处理结果。"""
    handled: bool = False
    blocked: bool = False
    block_reason: str = ""
    modified: bool = False
    modified_data: Any = None
    errors: list[str] = field(default_factory=list)


# Handler 类型
EventHandler = Callable[[Event], Awaitable[None]]
InterceptHandler = Callable[[Event], Awaitable[EventResult]]


class EventBus:
    """事件总线。"""

    def __init__(self):
        self._handlers: dict[EventType, list[EventHandler]] = {}
        self._intercept_handlers: dict[EventType, list[InterceptHandler]] = {}
        self._priority: dict[EventHandler, int] = {}

    def subscribe(
        self,
        event_type: EventType | str,
        handler: EventHandler | InterceptHandler,
        priority: int = 0,
    ) -> None:
        """订阅事件。

        Args:
            event_type: 事件类型
            handler: 处理函数
            priority: 优先级（越高越先执行）
        """
        if isinstance(event_type, str):
            event_type = EventType(event_type)

        if event_type in INTERCEPTABLE_EVENTS or event_type in MODIFIABLE_EVENTS:
            if event_type not in self._intercept_handlers:
                self._intercept_handlers[event_type] = []
            self._intercept_handlers[event_type].append(handler)
            self._priority[handler] = priority
        else:
            if event_type not in self._handlers:
                self._handlers[event_type] = []
            self._handlers[event_type].append(handler)
            self._priority[handler] = priority

    def unsubscribe(self, handler: EventHandler | InterceptHandler) -> None:
        """取消订阅。"""
        for handlers in self._handlers.values():
            if handler in handlers:
                handlers.remove(handler)
        for handlers in self._intercept_handlers.values():
            if handler in handlers:
                handlers.remove(handler)
        if handler in self._priority:
            del self._priority[handler]

    async def emit(self, event: Event) -> EventResult:
        """触发事件。

        Args:
            event: 事件对象

        Returns:
            事件处理结果
        """
        result = EventResult()

        # 获取 handlers
        handlers = self._handlers.get(event.type, [])
        intercept_handlers = self._intercept_handlers.get(event.type, [])

        # 按优先级排序
        all_handlers = [(h, self._priority.get(h, 0)) for h in handlers + intercept_handlers]
        all_handlers.sort(key=lambda x: x[1], reverse=True)

        for handler, _ in all_handlers:
            try:
                handler_result = await handler(event)

                # 处理拦截
                if event.blocked:
                    result.blocked = True
                    result.block_reason = event.block_reason
                    return result

                # 处理修改
                if event.modified:
                    result.modified = True
                    result.modified_data = event.modified_data

                # 处理 handler 返回值
                if isinstance(handler_result, EventResult):
                    if handler_result.blocked:
                        result.blocked = True
                        result.block_reason = handler_result.block_reason
                        return result
                    if handler_result.modified:
                        result.modified = True
                        result.modified_data = handler_result.modified_data

                result.handled = True

            except Exception as e:
                result.errors.append(str(e))

        return result

    async def emit_simple(self, event_type: EventType | str, **data: Any) -> EventResult:
        """简化版事件触发。"""
        if isinstance(event_type, str):
            event_type = EventType(event_type)
        event = Event(type=event_type, data=data)
        return await self.emit(event)

    def clear(self) -> None:
        """清除所有 handlers。"""
        self._handlers.clear()
        self._intercept_handlers.clear()
        self._priority.clear()

    def list_subscriptions(self) -> dict[str, int]:
        """列出所有订阅。"""
        result = {}
        for event_type, handlers in self._handlers.items():
            result[event_type.value] = len(handlers)
        for event_type, handlers in self._intercept_handlers.items():
            result[event_type.value] = result.get(event_type.value, 0) + len(handlers)
        return result
