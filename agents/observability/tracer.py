"""OTel Tracer — 直接在各处创建 span，独立于事件日志。

设计：
- 直接在各处使用 tracer.span() 创建 span
- 不依赖事件日志订阅者模式
- 支持上下文管理器自动管理 span 生命周期
- OTel 未启用时优雅降级
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Any, Iterator

_provider = None
_tracer = None


def init_tracer() -> None:
    """初始化 OTel TracerProvider，连接到 Phoenix"""
    global _provider, _tracer

    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

    endpoint = os.environ.get("MYCODE_OTEL_ENDPOINT", "http://localhost:4317")

    _provider = TracerProvider()
    exporter = OTLPSpanExporter(endpoint=endpoint)
    _provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(_provider)
    _tracer = _provider.get_tracer("mycode")


def tracer_enabled() -> bool:
    """检查 Tracer 是否已启用。"""
    return _tracer is not None


class Tracer:
    """OTel Tracer 封装类。"""
    
    def __init__(self):
        self._tracer = _tracer
    
    def is_enabled(self) -> bool:
        """检查 Tracer 是否已启用。"""
        return self._tracer is not None
    
    @contextmanager
    def span(self, name: str, attributes: dict[str, Any] | None = None) -> Iterator[Any]:
        """创建 span 的上下文管理器。
        
        OTel 未启用时 yield None。
        
        Args:
            name: span 名称
            attributes: span 属性
        
        Yields:
            OTel Span 对象，或 None（如果 OTel 未启用）
        """
        if not self.is_enabled():
            yield None
            return
        
        with self._tracer.start_as_current_span(name, attributes=attributes or {}) as span:
            try:
                yield span
            except Exception as e:
                span.set_status(status_code=2, description=str(e))  # ERROR = 2
                span.record_exception(e)
                raise
    
    def start_span(self, name: str, attributes: dict[str, Any] | None = None) -> Any:
        """手动创建 span（需要手动调用 end()）。
        
        Args:
            name: span 名称
            attributes: span 属性
        
        Returns:
            OTel Span 对象，或 None（如果 OTel 未启用）
        """
        if not self.is_enabled():
            return None
        
        span = self._tracer.start_span(name, attributes=attributes or {})
        return span


# 全局 Tracer 实例
tracer = Tracer()


# 便捷函数
def turn_span(turn_id: str, user_message: str):
    """Turn 级别 Span"""
    return tracer.span("turn", {
        "mycode.turn.id": turn_id,
        "mycode.turn.user_message": user_message[:500],
        "openinference.span.kind": "CHAIN",
    })


def model_call_span(model: str, input_tokens: int = 0, output_tokens: int = 0, cached_tokens: int = 0):
    """LLM 调用 Span"""
    import json
    attrs: dict[str, Any] = {
        "llm.invocation_parameters": json.dumps({"model": model}),
        "llm.token_count.prompt": input_tokens,
        "llm.token_count.completion": output_tokens,
        "openinference.span.kind": "LLM",
    }
    if cached_tokens > 0:
        attrs["mycode.tokens.cached"] = cached_tokens
    return tracer.span(f"llm.{model}", attrs)


def tool_call_span(tool_name: str, tool_input: str = ""):
    """工具调用 Span"""
    return tracer.span(f"tool.{tool_name}", {
        "tool.name": tool_name,
        "tool.parameters": tool_input[:1000],
        "openinference.span.kind": "TOOL",
    })


def sub_agent_span(agent_type: str, agent_name: str):
    """子智能体 Span"""
    return tracer.span(f"sub_agent.{agent_type}", {
        "mycode.agent.type": agent_type,
        "mycode.agent.name": agent_name,
        "openinference.span.kind": "AGENT",
    })


def audit_span(decision: str, tool: str, risk_level: str = "low"):
    """审计 Span"""
    return tracer.span(f"audit.{tool}", {
        "mycode.audit.decision": decision,
        "mycode.audit.risk_level": risk_level,
        "openinference.span.kind": "GUARDRAIL",
    })


def compaction_span(before_tokens: int, after_tokens: int):
    """上下文压缩 Span"""
    ratio = after_tokens / before_tokens if before_tokens > 0 else 0
    return tracer.span("context.compaction", {
        "mycode.context.tokens_before": before_tokens,
        "mycode.context.tokens_after": after_tokens,
        "mycode.context.compression_ratio": round(ratio, 4),
        "mycode.context.tokens_saved": before_tokens - after_tokens,
        "openinference.span.kind": "CHAIN",
    })
