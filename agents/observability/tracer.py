"""OTel Tracer — 直接在各处创建 span，导出到 Langfuse（OTLP/HTTP）。

设计：
- 直接在各处使用 tracer.span() 创建 span
- 不依赖事件日志订阅者模式
- 支持上下文管理器自动管理 span 生命周期
- OTel 未启用时优雅降级

Langfuse 接入规范（官方文档 langfuse.com/docs/opentelemetry/get-started）：
- 端点：{LANGFUSE_BASE_URL}/api/public/otel/v1/traces（仅支持 OTLP/HTTP，不支持 gRPC）
- 认证：Authorization: Basic base64(public_key:secret_key)
- 实时摄取：x-langfuse-ingestion-version: 4（否则延迟最多 10 分钟）
- observation 类型：langfuse.observation.type =
    generation(LLM) / tool(工具) / agent(子智能体) / guardrail(审计) / chain(编排)
- 会话关联：langfuse.session.id 需传播到 trace 内所有 span
- 可过滤元数据：langfuse.observation.metadata.* 前缀（否则落入 catch-all 不可过滤）
"""

from __future__ import annotations

import base64
import contextvars
import json
import os
from contextlib import contextmanager
from typing import Any, Iterator

_provider = None
_tracer = None

# session_id 上下文变量：turn 开始时设置，所有子 span 自动携带
_current_session_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "mycode_session_id", default=""
)


def set_current_session_id(session_id: str) -> None:
    _current_session_id.set(session_id)


def _session_attrs() -> dict[str, Any]:
    sid = _current_session_id.get()
    return {"langfuse.session.id": sid} if sid else {}


def init_tracer() -> None:
    """初始化 OTel TracerProvider，通过 OTLP/HTTP 导出到 Langfuse。"""
    global _provider, _tracer

    from opentelemetry import trace
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

    public_key = os.environ.get("LANGFUSE_PUBLIC_KEY", "")
    secret_key = os.environ.get("LANGFUSE_SECRET_KEY", "")
    if not public_key or not secret_key:
        raise RuntimeError(
            "LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY 未配置，无法初始化 OTel（请检查 .env）"
        )

    auth = base64.b64encode(f"{public_key}:{secret_key}".encode()).decode()
    endpoint = os.environ.get(
        "MYCODE_OTEL_ENDPOINT",
        f"{os.environ.get('LANGFUSE_BASE_URL', 'https://us.cloud.langfuse.com')}/api/public/otel/v1/traces",
    )

    resource = Resource.create({"service.name": "mycode"})
    _provider = TracerProvider(resource=resource)
    exporter = OTLPSpanExporter(
        endpoint=endpoint,
        headers={
            "Authorization": f"Basic {auth}",
            "x-langfuse-ingestion-version": "4",
        },
    )
    _provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(_provider)
    _tracer = _provider.get_tracer("mycode")


def flush_tracer() -> None:
    """强制导出当前批次的所有 span（不关闭 provider）。

    评测 runner 逐任务 flush 后立即回查 Langfuse 时使用。
    """
    if _provider is not None:
        _provider.force_flush()


def shutdown_tracer() -> None:
    """进程退出前 flush 所有未发送的 span（脚本场景必须调用，否则 trace 丢失）。"""
    if _provider is not None:
        _provider.force_flush()
        _provider.shutdown()


def tracer_enabled() -> bool:
    """检查 Tracer 是否已启用。"""
    return _tracer is not None


class Tracer:
    """OTel Tracer 封装类（动态读取全局 _tracer，init 顺序无关）。"""

    def is_enabled(self) -> bool:
        """检查 Tracer 是否已启用。"""
        return _tracer is not None

    @contextmanager
    def span(self, name: str, attributes: dict[str, Any] | None = None) -> Iterator[Any]:
        """创建 span 的上下文管理器。

        OTel 未启用时 yield None。
        session_id 自动附加到所有 span（Langfuse 要求 trace 级属性传播到每个 span）。
        """
        if _tracer is None:
            yield None
            return

        attrs = {**_session_attrs(), **(attributes or {})}
        with _tracer.start_as_current_span(name, attributes=attrs) as span:
            try:
                yield span
            except Exception as e:
                span.set_status(status_code=2, description=str(e))  # ERROR = 2
                span.record_exception(e)
                raise

    def start_span(self, name: str, attributes: dict[str, Any] | None = None) -> Any:
        """手动创建 span（需要手动调用 end()）。"""
        if _tracer is None:
            return None

        attrs = {**_session_attrs(), **(attributes or {})}
        return _tracer.start_span(name, attributes=attrs)


# 全局 Tracer 实例
tracer = Tracer()


# 便捷函数

def turn_span(turn_id: str, user_message: str):
    """Turn 级别 Span（chain 类型，trace 根节点）。"""
    return tracer.span("turn", {
        "langfuse.observation.type": "chain",
        "langfuse.trace.name": "mycode-turn",
        "mycode.turn.id": turn_id,
        "langfuse.observation.input": user_message[:2000],
        "langfuse.observation.metadata.turn_user_message": user_message[:500],
    })


def model_call_span(model: str, input_tokens: int = 0, output_tokens: int = 0, cached_tokens: int = 0):
    """LLM 调用 Span（generation 类型）。

    llm.token_count.* 为 Langfuse 官方支持的 usage 映射属性（OpenInference 兼容）。
    """
    attrs: dict[str, Any] = {
        "langfuse.observation.type": "generation",
        "langfuse.observation.model.name": model,
        "llm.token_count.prompt": input_tokens,
        "llm.token_count.completion": output_tokens,
    }
    if cached_tokens > 0:
        attrs["mycode.tokens.cached"] = cached_tokens
    return tracer.span(f"llm.{model}", attrs)


def tool_call_span(tool_name: str, tool_input: str = ""):
    """工具调用 Span（tool 类型）。"""
    return tracer.span(f"tool.{tool_name}", {
        "langfuse.observation.type": "tool",
        "tool.name": tool_name,
        "langfuse.observation.input": tool_input[:1000],
    })


def sub_agent_span(agent_type: str, agent_name: str):
    """子智能体 Span（agent 类型，Langfuse Agent Graph 节点）。"""
    return tracer.span(f"sub_agent.{agent_name or agent_type}", {
        "langfuse.observation.type": "agent",
        "mycode.agent.type": agent_type,
        "mycode.agent.name": agent_name,
    })


def audit_span(decision: str, tool: str, risk_level: str = "low"):
    """审计 Span（guardrail 类型）。"""
    return tracer.span(f"audit.{tool}", {
        "langfuse.observation.type": "guardrail",
        "langfuse.observation.metadata.audit_decision": decision,
        "langfuse.observation.metadata.audit_risk_level": risk_level,
    })


def compaction_span(before_tokens: int, after_tokens: int):
    """上下文压缩 Span（chain 类型）。"""
    ratio = after_tokens / before_tokens if before_tokens > 0 else 0
    return tracer.span("context.compaction", {
        "langfuse.observation.type": "chain",
        "langfuse.observation.metadata.tokens_before": before_tokens,
        "langfuse.observation.metadata.tokens_after": after_tokens,
        "langfuse.observation.metadata.compression_ratio": round(ratio, 4),
        "langfuse.observation.metadata.tokens_saved": before_tokens - after_tokens,
    })
