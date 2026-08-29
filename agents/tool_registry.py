#!/usr/bin/env python3
from __future__ import annotations

from typing import Any, Callable

from agents.hooks import ToolCallContext, global_hooks


ToolDef = dict[str, Any]


class ToolRegistry:
    """统一工具注册表"""
    
    def __init__(self):
        self._tools: dict[str, ToolDef] = {}
        self._sources: dict[str, str] = {}
        self._handlers: dict[str, Callable] = {}
    
    def register(self, tool_def: ToolDef, handler: Callable, source: str = "builtin") -> None:
        """注册内置工具"""
        name = tool_def["name"]
        if name in self._tools:
            raise ValueError(f"Tool '{name}' already registered from {self._sources[name]}")
        self._tools[name] = tool_def
        self._sources[name] = source
        self._handlers[name] = handler
    
    def register_mcp(self, tool_def: ToolDef, server: str) -> None:
        """注册 MCP 工具（自动加前缀）"""
        original_name = tool_def["name"]
        name = f"mcp__{server}__{original_name}"
        self._tools[name] = tool_def
        self._sources[name] = f"mcp:{server}"
    
    def register_extension(self, tool_def: ToolDef, handler: Callable, extension: str) -> None:
        """注册扩展工具"""
        name = tool_def["name"]
        self._tools[name] = tool_def
        self._sources[name] = f"extension:{extension}"
        self._handlers[name] = handler
    
    def get(self, name: str) -> ToolDef | None:
        """获取工具定义"""
        return self._tools.get(name)
    
    def get_handler(self, name: str) -> Callable | None:
        """获取工具处理器"""
        return self._handlers.get(name)
    
    def list_all(self) -> list[ToolDef]:
        """列出所有工具"""
        return list(self._tools.values())
    
    def list_by_source(self, source: str) -> list[ToolDef]:
        """按来源列出工具"""
        return [t for n, t in self._tools.items() if self._sources.get(n, "").startswith(source)]
    
    async def dispatch(self, name: str, input: dict, ctx: ToolCallContext) -> Any:
        """分发工具执行（带 Hook）"""
        if not await global_hooks.run_before(ctx):
            return {"cancelled": True, "reason": "Hook cancelled execution"}
        
        handler = self.get_handler(name)
        if handler:
            result = handler(input)
        else:
            raise ValueError(f"No handler for tool '{name}'")
        
        result = await global_hooks.run_after(ctx, result)
        
        return result
    
    def unregister(self, name: str) -> bool:
        """注销工具"""
        if name in self._tools:
            del self._tools[name]
            del self._sources[name]
            self._handlers.pop(name, None)
            return True
        return False
    
    def clear(self) -> None:
        """清除所有工具（测试用）"""
        self._tools.clear()
        self._sources.clear()
        self._handlers.clear()


global_registry = ToolRegistry()
