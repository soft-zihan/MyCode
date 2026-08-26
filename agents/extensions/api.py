"""Extension API for registering handlers, tools, and commands.

扩展示例:
    def setup(ext: ExtensionAPI):
        @ext.on("tool_call")
        def block_dangerous(event):
            if event.data.get("tool_name") == "run_shell":
                cmd = event.data.get("command", "")
                if "rm -rf" in cmd:
                    event.block("Blocked dangerous command")

        @ext.on("before_compact")
        def custom_summary(event):
            # 自定义压缩逻辑
            pass

        ext.register_tool({
            "name": "my_tool",
            "description": "My custom tool",
            "parameters": {...},
            "handler": my_tool_handler,
        })

        ext.register_command("mycommand", my_command_handler)
"""

from __future__ import annotations

from typing import Any, Callable, Awaitable

from .event_bus import EventBus, Event, EventType, EventResult


class ExtensionAPI:
    """扩展 API，提供给扩展使用的接口。"""

    def __init__(self, extension_name: str, event_bus: EventBus):
        self.extension_name = extension_name
        self.event_bus = event_bus
        self._registered_handlers: list[Callable] = []
        self._registered_tools: list[str] = []
        self._registered_commands: list[str] = []
        self._tool_registry: dict[str, dict[str, Any]] = {}
        self._command_registry: dict[str, Callable] = {}

    def on(
        self,
        event_type: EventType | str,
        priority: int = 0,
    ) -> Callable:
        """订阅事件的装饰器。

        Usage:
            @ext.on("tool_call")
            def handler(event):
                ...
        """
        def decorator(handler: Callable) -> Callable:
            self.event_bus.subscribe(event_type, handler, priority)
            self._registered_handlers.append(handler)
            return handler
        return decorator

    def subscribe(
        self,
        event_type: EventType | str,
        handler: Callable,
        priority: int = 0,
    ) -> None:
        """订阅事件。"""
        self.event_bus.subscribe(event_type, handler, priority)
        self._registered_handlers.append(handler)

    def unsubscribe(self, handler: Callable) -> None:
        """取消订阅。"""
        self.event_bus.unsubscribe(handler)
        if handler in self._registered_handlers:
            self._registered_handlers.remove(handler)

    def register_tool(self, tool_def: dict[str, Any]) -> None:
        """注册自定义工具。

        Args:
            tool_def: 工具定义，必须包含:
                - name: 工具名称
                - description: 工具描述
                - parameters: JSON Schema 参数定义
                - handler: 处理函数 (async def handler(**kwargs) -> Any)
        """
        name = tool_def.get("name")
        if not name:
            raise ValueError("Tool must have a name")

        if "handler" not in tool_def:
            raise ValueError("Tool must have a handler")

        self._tool_registry[name] = tool_def
        self._registered_tools.append(name)

    def register_command(self, name: str, handler: Callable) -> None:
        """注册自定义命令。

        Args:
            name: 命令名称（不含 /）
            handler: 处理函数 (async def handler(args: str) -> str)
        """
        self._command_registry[name] = handler
        self._registered_commands.append(name)

    def get_tools(self) -> dict[str, dict[str, Any]]:
        """获取此扩展注册的所有工具。"""
        return {name: self._tool_registry[name] for name in self._registered_tools}

    def get_commands(self) -> dict[str, Callable]:
        """获取此扩展注册的所有命令。"""
        return {name: self._command_registry[name] for name in self._registered_commands}

    def unregister_all(self) -> None:
        """取消注册所有内容。"""
        # 取消事件订阅
        for handler in self._registered_handlers:
            self.event_bus.unsubscribe(handler)
        self._registered_handlers.clear()

        # 清除工具注册
        for name in self._registered_tools:
            if name in self._tool_registry:
                del self._tool_registry[name]
        self._registered_tools.clear()

        # 清除命令注册
        for name in self._registered_commands:
            if name in self._command_registry:
                del self._command_registry[name]
        self._registered_commands.clear()

    # ─── Helper Methods ───────────────────────────────────────────────────────

    def log(self, message: str, level: str = "info") -> None:
        """记录扩展日志。"""
        from .trace import trace_event
        trace_event("extension.log", extension=self.extension_name, level=level, message=message)

    def get_config(self, key: str, default: Any = None) -> Any:
        """获取扩展配置。"""
        # TODO: 实现配置存储
        return default

    def set_config(self, key: str, value: Any) -> None:
        """设置扩展配置。"""
        # TODO: 实现配置存储
        pass


# ─── Global Tool/Command Registry ─────────────────────────────────────────────


class GlobalRegistry:
    """全局工具和命令注册表。"""

    def __init__(self):
        self._tools: dict[str, dict[str, Any]] = {}
        self._commands: dict[str, Callable] = {}

    def register_tool(self, tool_def: dict[str, Any]) -> None:
        """注册工具。"""
        name = tool_def.get("name")
        if name:
            self._tools[name] = tool_def

    def register_command(self, name: str, handler: Callable) -> None:
        """注册命令。"""
        self._commands[name] = handler

    def get_tool(self, name: str) -> dict[str, Any] | None:
        """获取工具定义。"""
        return self._tools.get(name)

    def get_command(self, name: str) -> Callable | None:
        """获取命令处理函数。"""
        return self._commands.get(name)

    def list_tools(self) -> list[str]:
        """列出所有工具。"""
        return list(self._tools.keys())

    def list_commands(self) -> list[str]:
        """列出所有命令。"""
        return list(self._commands.keys())

    def unregister_tool(self, name: str) -> bool:
        """注销工具。"""
        if name in self._tools:
            del self._tools[name]
            return True
        return False

    def unregister_command(self, name: str) -> bool:
        """注销命令。"""
        if name in self._commands:
            del self._commands[name]
            return True
        return False


_global_registry: GlobalRegistry | None = None


def get_global_registry() -> GlobalRegistry:
    """获取全局注册表。"""
    global _global_registry
    if _global_registry is None:
        _global_registry = GlobalRegistry()
    return _global_registry
