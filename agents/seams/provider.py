"""Provider Seam 接口定义。

LLM Provider 抽象。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Protocol


# ============================================================
# 数据类型
# ============================================================


@dataclass
class ProviderResult:
    """Provider 返回结果，包含统计信息。"""
    
    content: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)
    ttft_ms: float = 0
    
    @property
    def input_tokens(self) -> int:
        return self.usage.get("input_tokens", 0)
    
    @property
    def output_tokens(self) -> int:
        return self.usage.get("output_tokens", 0)
    
    @property
    def cache_hit(self) -> bool:
        return self.usage.get("cache_hit", False)


@dataclass
class StreamEvent:
    """流式事件。"""
    
    type: str  # "text" | "tool_use" | "tool_result" | "error" | "done"
    content: Any = None
    tool_name: str | None = None
    tool_input: dict[str, Any] | None = None
    tool_output: Any = None
    error: str | None = None


# ============================================================
# Provider 接口
# ============================================================


class Provider(Protocol):
    """LLM Provider 协议。"""
    
    async def stream(
        self,
        messages: list[dict[str, Any]],
        *,
        system: str = "",
        tools: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[StreamEvent]: ...
    
    async def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        system: str = "",
        tools: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]: ...
    
    def count_tokens(
        self,
        messages: list[dict[str, Any]],
        *,
        system: str = "",
    ) -> int: ...
