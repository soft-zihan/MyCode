"""Agent 配置选项。

AgentOptions 封装了 Agent 的所有配置参数，简化构造过程。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable

from agents.tools import ToolDef


@dataclass
class AgentOptions:
    """Agent 配置选项。
    
    所有字段都有默认值，可以按需覆盖。
    """
    
    # 权限模式
    permission_mode: str = "default"
    
    # 模型配置
    model: str = "deepseek-chat"
    api_base: str | None = None
    anthropic_base_url: str | None = None
    api_key: str | None = None
    
    # 思考模式
    thinking: bool | None = None  # None=跟随模型默认
    thinking_feedback: bool | None = None  # None=跟随端点配置; True=历史思考以 reasoning_content 回传

    # 压缩对比实验臂（消融）：None/full=现状, truncate=朴素硬截断, tool_only, session_only
    compression_arm: str | None = None
    context_window: int | None = None
    
    # 成本、轮次和工具调用限制
    max_cost_usd: float | None = None
    max_turns: int | None = None
    max_tool_calls: int | None = None
    
    # 确认函数
    confirm_fn: Callable[[str], Awaitable[bool]] | None = None
    
    # 系统提示词
    custom_system_prompt: str | None = None
    
    # 工具定义
    custom_tools: list[ToolDef] | None = None
    
    # 子 Agent 配置
    is_sub_agent: bool = False
    parent_abort_event: asyncio.Event | None = None
    
    # 工作区（会话工作目录；None 时回退进程 CWD，仅 CLI 场景）
    workspace: Any | None = None
    
    # Checkpoint 存储
    checkpoint_store: Any | None = None
    
    # 其他选项
    extra: dict[str, Any] = field(default_factory=dict)
    
    def to_dict(self) -> dict[str, Any]:
        """转换为字典。"""
        return {
            "permission_mode": self.permission_mode,
            "model": self.model,
            "api_base": self.api_base,
            "anthropic_base_url": self.anthropic_base_url,
            "api_key": self.api_key,
            "thinking": self.thinking,
            "thinking_feedback": self.thinking_feedback,
            "compression_arm": self.compression_arm,
            "context_window": self.context_window,
            "max_cost_usd": self.max_cost_usd,
            "max_turns": self.max_turns,
            "max_tool_calls": self.max_tool_calls,
            "confirm_fn": self.confirm_fn,
            "custom_system_prompt": self.custom_system_prompt,
            "custom_tools": self.custom_tools,
            "is_sub_agent": self.is_sub_agent,
            "parent_abort_event": self.parent_abort_event,
            "workspace": self.workspace,
            "checkpoint_store": self.checkpoint_store,
            "extra": self.extra,
        }
    
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AgentOptions:
        """从字典创建。"""
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
    
    def with_overrides(self, **kwargs: Any) -> AgentOptions:
        """创建带有覆盖选项的新实例。"""
        data = self.to_dict()
        data.update(kwargs)
        return AgentOptions.from_dict(data)
