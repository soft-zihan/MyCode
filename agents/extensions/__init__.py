"""Extension event system for MyCode.

Features:
- 20+ 生命周期事件订阅
- 自定义工具注册
- 自定义命令注册
- 热重载支持

事件列表:
- session_start / session_end
- input (可拦截)
- before_agent_start / agent_start / agent_end
- turn_start / turn_end
- tool_call (可拦截) / tool_result (可修改)
- before_provider_request (可修改) / after_provider_response
- before_compact (可拦截) / after_compact
- before_skill_evolution (可拦截) / after_skill_evolution
- memory_recall (可过滤)
- model_select
"""

from __future__ import annotations

from .event_bus import EventBus, Event, EventResult
from .loader import ExtensionLoader, Extension
from .api import ExtensionAPI

__all__ = [
    "EventBus",
    "Event",
    "EventResult",
    "ExtensionLoader",
    "Extension",
    "ExtensionAPI",
]
