"""Provider Seam 接口定义。

LLM Provider 抽象，支持多种实现：
- OpenAIProvider：OpenAI API
- AnthropicProvider：Anthropic API
- CustomProvider：自定义 Provider
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Protocol


# ============================================================
# 数据类型
# ============================================================


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
    """LLM Provider 协议。
    
    所有 Provider 实现必须遵守此接口。
    """
    
    async def stream(
        self,
        messages: list[dict[str, Any]],
        *,
        system: str = "",
        tools: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[StreamEvent]:
        """流式调用 LLM。
        
        Args:
            messages: 消息列表
            system: 系统提示词
            tools: 工具定义列表
            **kwargs: 其他参数
        
        Yields:
            StreamEvent: 流式事件
        """
        ...
    
    async def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        system: str = "",
        tools: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """同步调用 LLM（等待完整响应）。
        
        Args:
            messages: 消息列表
            system: 系统提示词
            tools: 工具定义列表
            **kwargs: 其他参数
        
        Returns:
            完整响应
        """
        ...
    
    def count_tokens(
        self,
        messages: list[dict[str, Any]],
        *,
        system: str = "",
    ) -> int:
        """计算 token 数。
        
        Args:
            messages: 消息列表
            system: 系统提示词
        
        Returns:
            token 数
        """
        ...


# ============================================================
# OpenAI Provider 实现
# ============================================================


class OpenAIProvider:
    """OpenAI API Provider。"""
    
    def __init__(
        self,
        api_key: str,
        model: str = "gpt-4",
        base_url: str | None = None,
        **kwargs: Any,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._base_url = base_url
        self._kwargs = kwargs
    
    async def stream(
        self,
        messages: list[dict[str, Any]],
        *,
        system: str = "",
        tools: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[StreamEvent]:
        """流式调用 OpenAI API。"""
        from openai import AsyncOpenAI
        
        client = AsyncOpenAI(
            api_key=self._api_key,
            base_url=self._base_url,
        )
        
        # 构建请求参数
        params = {
            "model": self._model,
            "messages": [{"role": "system", "content": system}] + messages if system else messages,
            "stream": True,
        }
        if tools:
            params["tools"] = tools
        params.update(self._kwargs)
        params.update(kwargs)
        
        # 流式调用
        response = await client.chat.completions.create(**params)
        
        async for chunk in response:
            if chunk.choices and chunk.choices[0].delta:
                delta = chunk.choices[0].delta
                
                # 文本内容
                if delta.content:
                    yield StreamEvent(type="text", content=delta.content)
                
                # 工具调用
                if delta.tool_calls:
                    for tool_call in delta.tool_calls:
                        if tool_call.function:
                            yield StreamEvent(
                                type="tool_use",
                                tool_name=tool_call.function.name,
                                tool_input=tool_call.function.arguments,
                            )
        
        yield StreamEvent(type="done")
    
    async def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        system: str = "",
        tools: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """同步调用 OpenAI API。"""
        from openai import AsyncOpenAI
        
        client = AsyncOpenAI(
            api_key=self._api_key,
            base_url=self._base_url,
        )
        
        params = {
            "model": self._model,
            "messages": [{"role": "system", "content": system}] + messages if system else messages,
        }
        if tools:
            params["tools"] = tools
        params.update(self._kwargs)
        params.update(kwargs)
        
        response = await client.chat.completions.create(**params)
        
        result = {
            "content": response.choices[0].message.content or "",
            "tool_calls": [],
        }
        
        if response.choices[0].message.tool_calls:
            for tool_call in response.choices[0].message.tool_calls:
                result["tool_calls"].append({
                    "name": tool_call.function.name,
                    "input": tool_call.function.arguments,
                })
        
        return result
    
    def count_tokens(
        self,
        messages: list[dict[str, Any]],
        *,
        system: str = "",
    ) -> int:
        """计算 token 数（简化实现）。"""
        # 简化：按字符数估算
        total = len(system)
        for msg in messages:
            total += len(str(msg.get("content", "")))
        return total // 4  # 粗略估算


# ============================================================
# Anthropic Provider 实现
# ============================================================


class AnthropicProvider:
    """Anthropic API Provider。"""
    
    def __init__(
        self,
        api_key: str,
        model: str = "claude-3-5-sonnet-20241022",
        **kwargs: Any,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._kwargs = kwargs
    
    async def stream(
        self,
        messages: list[dict[str, Any]],
        *,
        system: str = "",
        tools: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[StreamEvent]:
        """流式调用 Anthropic API。"""
        from anthropic import AsyncAnthropic
        
        client = AsyncAnthropic(api_key=self._api_key)
        
        params = {
            "model": self._model,
            "messages": messages,
            "system": system,
            "max_tokens": 8192,
            "stream": True,
        }
        if tools:
            params["tools"] = tools
        params.update(self._kwargs)
        params.update(kwargs)
        
        response = await client.messages.create(**params)
        
        async for event in response:
            if event.type == "content_block_delta":
                if event.delta.type == "text_delta":
                    yield StreamEvent(type="text", content=event.delta.text)
                elif event.delta.type == "tool_use_delta":
                    yield StreamEvent(
                        type="tool_use",
                        tool_name=event.delta.name,
                        tool_input=event.delta.input,
                    )
            elif event.type == "message_stop":
                yield StreamEvent(type="done")
    
    async def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        system: str = "",
        tools: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """同步调用 Anthropic API。"""
        from anthropic import AsyncAnthropic
        
        client = AsyncAnthropic(api_key=self._api_key)
        
        params = {
            "model": self._model,
            "messages": messages,
            "system": system,
            "max_tokens": 8192,
        }
        if tools:
            params["tools"] = tools
        params.update(self._kwargs)
        params.update(kwargs)
        
        response = await client.messages.create(**params)
        
        result = {
            "content": "",
            "tool_calls": [],
        }
        
        for block in response.content:
            if block.type == "text":
                result["content"] += block.text
            elif block.type == "tool_use":
                result["tool_calls"].append({
                    "name": block.name,
                    "input": block.input,
                })
        
        return result
    
    def count_tokens(
        self,
        messages: list[dict[str, Any]],
        *,
        system: str = "",
    ) -> int:
        """计算 token 数（简化实现）。"""
        # 简化：按字符数估算
        total = len(system)
        for msg in messages:
            total += len(str(msg.get("content", "")))
        return total // 4  # 粗略估算
