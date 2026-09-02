"""OTel Span 导出器 — 将 MyCode 事件转为 OTel Spans 发送到 Phoenix

启用条件：BEAR_OTEL=1
默认端点：http://localhost:4317（Phoenix OTLP gRPC）

依赖：pip install opentelemetry-api opentelemetry-sdk opentelemetry-exporter-otlp-proto-grpc
"""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from typing import Any

_provider = None
_tracer = None


def init_otel() -> None:
    """初始化 OTel TracerProvider，连接到 Phoenix"""
    global _provider, _tracer

    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

    endpoint = os.environ.get("BEAR_OTEL_ENDPOINT", "http://localhost:4317")

    _provider = TracerProvider()
    exporter = OTLPSpanExporter(endpoint=endpoint)
    _provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(_provider)
    _tracer = _provider.get_tracer("MyCode")


def otel_enabled() -> bool:
    return _tracer is not None


@contextmanager
def otel_span(kind: str, attributes: dict[str, Any] | None = None):
    """创建 OTel Span 的上下文管理器。OTel 未启用时 yield None。"""
    if not otel_enabled():
        yield None
        return

    with _tracer.start_as_current_span(kind, attributes=attributes or {}) as span:
        yield span


def otel_turn(turn_id: str, user_message: str):
    """Turn 级别 Span"""
    return otel_span("turn", {
        "MyCode.turn.id": turn_id,
        "MyCode.turn.user_message": user_message[:500],
        "openinference.span.kind": "CHAIN",
    })


def otel_model_call(model: str, input_tokens: int = 0, output_tokens: int = 0, cached_tokens: int = 0):
    """LLM 调用 Span"""
    attrs: dict[str, Any] = {
        "llm.invocation_parameters": json.dumps({"model": model}),
        "llm.token_count.prompt": input_tokens,
        "llm.token_count.completion": output_tokens,
        "openinference.span.kind": "LLM",
    }
    if cached_tokens > 0:
        attrs["MyCode.tokens.cached"] = cached_tokens
    return otel_span(f"llm.{model}", attrs)


def otel_tool_call(tool_name: str, tool_input: str = ""):
    """工具调用 Span"""
    return otel_span(f"tool.{tool_name}", {
        "tool.name": tool_name,
        "tool.parameters": tool_input[:1000],
        "openinference.span.kind": "TOOL",
    })


def otel_sub_agent(agent_type: str, agent_name: str):
    """子智能体 Span"""
    return otel_span(f"sub_agent.{agent_type}", {
        "MyCode.agent.type": agent_type,
        "MyCode.agent.name": agent_name,
        "openinference.span.kind": "AGENT",
    })


def otel_audit(decision: str, tool: str, risk_level: str = "low"):
    """审计 Span"""
    return otel_span(f"audit.{tool}", {
        "MyCode.audit.decision": decision,
        "MyCode.audit.risk_level": risk_level,
        "openinference.span.kind": "GUARDRAIL",
    })


def otel_compaction(before_tokens: int, after_tokens: int):
    """上下文压缩 Span"""
    ratio = after_tokens / before_tokens if before_tokens > 0 else 0
    return otel_span("context.compaction", {
        "MyCode.context.tokens_before": before_tokens,
        "MyCode.context.tokens_after": after_tokens,
        "MyCode.context.compression_ratio": round(ratio, 4),
        "MyCode.context.tokens_saved": before_tokens - after_tokens,
        "openinference.span.kind": "CHAIN",
    })
