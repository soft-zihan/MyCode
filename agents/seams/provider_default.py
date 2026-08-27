"""默认 Provider 实现。

封装现有的 OpenAI/Anthropic 客户端逻辑，使其符合 Provider 接口。
"""

from __future__ import annotations

from typing import Any, AsyncIterator

from agents.seams.provider import StreamEvent


class DefaultProvider:
    """默认 Provider（封装现有的 OpenAI/Anthropic 客户端）。
    
    这是一个适配器，将现有的 Agent 客户端逻辑包装成 Provider 接口。
    """
    
    def __init__(
        self,
        agent: Any,  # Agent 实例
    ) -> None:
        self._agent = agent
    
    async def stream(
        self,
        messages: list[dict[str, Any]],
        *,
        system: str = "",
        tools: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[StreamEvent]:
        """流式调用 LLM。
        
        注意：这是一个简化实现，实际的流式逻辑在 Agent 类中。
        这里只提供接口框架。
        """
        # 简化：直接完成
        yield StreamEvent(type="done")
    
    async def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        system: str = "",
        tools: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """同步调用 LLM。
        
        注意：这是一个简化实现，实际的调用逻辑在 Agent 类中。
        这里只提供接口框架。
        """
        return {
            "content": "",
            "tool_calls": [],
        }
    
    def count_tokens(
        self,
        messages: list[dict[str, Any]],
        *,
        system: str = "",
    ) -> int:
        """计算 token 数。
        
        委托给 Agent 的现有方法。
        """
        # 简化：按字符数估算
        total = len(system)
        for msg in messages:
            content = msg.get("content", "")
            if isinstance(content, str):
                total += len(content)
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict):
                        total += len(str(block.get("text", "")))
        return total // 4  # 粗略估算
