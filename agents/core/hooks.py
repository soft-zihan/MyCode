#!/usr/bin/env python3
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Callable


@dataclass
class ToolCallContext:
    """工具调用上下文"""
    tool_name: str
    tool_input: dict
    agent_id: str
    session_id: str


class HookRegistry:
    """钩子注册表"""
    
    def __init__(self):
        self._before_hooks: list[Callable[[ToolCallContext], Any]] = []
        self._after_hooks: list[Callable[[ToolCallContext, Any], Any]] = []
    
    def before_tool_call(self, fn: Callable[[ToolCallContext], Any]) -> None:
        """注册工具执行前的钩子"""
        self._before_hooks.append(fn)
    
    def after_tool_call(self, fn: Callable[[ToolCallContext, Any], Any]) -> None:
        """注册工具执行后的钩子"""
        self._after_hooks.append(fn)
    
    async def run_before(self, ctx: ToolCallContext) -> bool:
        """执行所有 before hooks，返回 False 表示取消执行"""
        for hook in self._before_hooks:
            result = await hook(ctx) if asyncio.iscoroutinefunction(hook) else hook(ctx)
            if result is False:
                return False
        return True
    
    async def run_after(self, ctx: ToolCallContext, result: Any) -> Any:
        """执行所有 after hooks，可以修改结果"""
        for hook in self._after_hooks:
            new_result = await hook(ctx, result) if asyncio.iscoroutinefunction(hook) else hook(ctx, result)
            if new_result is not None:
                result = new_result
        return result
    
    def clear(self) -> None:
        """清除所有钩子（测试用）"""
        self._before_hooks.clear()
        self._after_hooks.clear()


global_hooks = HookRegistry()
