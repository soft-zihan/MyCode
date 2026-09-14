"""OTel Span 导出器 — 将 MyCode 事件转为 OTel Spans 发送到 Langfuse（OTLP/HTTP）

启用条件：MYCODE_OTEL=1 + LANGFUSE_PUBLIC_KEY/LANGFUSE_SECRET_KEY（.env）
默认端点：{LANGFUSE_BASE_URL}/api/public/otel/v1/traces

依赖：pip install opentelemetry-api opentelemetry-sdk opentelemetry-exporter-otlp-proto-http
"""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from typing import Any

# 导入新的 Tracer 模块
from .tracer import (
    init_tracer,
    shutdown_tracer,
    set_current_session_id,
    tracer_enabled,
    tracer,
    turn_span,
    model_call_span,
    tool_call_span,
    sub_agent_span,
    audit_span,
    compaction_span,
)


def init_otel() -> None:
    """初始化 OTel TracerProvider，连接到 Langfuse"""
    init_tracer()


def otel_enabled() -> bool:
    return tracer_enabled()


@contextmanager
def otel_span(kind: str, attributes: dict[str, Any] | None = None):
    """创建 OTel Span 的上下文管理器。OTel 未启用时 yield None。"""
    with tracer.span(kind, attributes) as span:
        yield span


def otel_turn(turn_id: str, user_message: str):
    """Turn 级别 Span"""
    return turn_span(turn_id, user_message)


def otel_model_call(model: str, input_tokens: int = 0, output_tokens: int = 0, cached_tokens: int = 0):
    """LLM 调用 Span"""
    return model_call_span(model, input_tokens, output_tokens, cached_tokens)


def otel_tool_call(tool_name: str, tool_input: str = ""):
    """工具调用 Span"""
    return tool_call_span(tool_name, tool_input)


def otel_sub_agent(agent_type: str, agent_name: str):
    """子智能体 Span"""
    return sub_agent_span(agent_type, agent_name)


def otel_audit(decision: str, tool: str, risk_level: str = "low"):
    """审计 Span"""
    return audit_span(decision, tool, risk_level)


def otel_compaction(before_tokens: int, after_tokens: int):
    """上下文压缩 Span"""
    return compaction_span(before_tokens, after_tokens)
