from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import agents.observability.tracer as tracer_mod
from agents.observability.trace import (
    resolve_observation_type,
    start_trace_span,
    trace_context,
    trace_event,
    trace_span,
    tracing_enabled,
)


def _unique_key(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _attributes(spans: list[Any], name: str) -> dict[str, Any]:
    matching = [span for span in spans if span.name == name]
    assert len(matching) == 1
    return dict(matching[0].attributes or {})


@pytest.fixture(autouse=True)
def reset_trace_client():
    tracer_mod.shutdown_trace_client()
    yield
    tracer_mod.shutdown_trace_client()


@pytest.fixture
def langfuse_exporter(monkeypatch: pytest.MonkeyPatch) -> InMemorySpanExporter:
    monkeypatch.setenv("MYCODE_TRACING", "1")
    monkeypatch.setenv("LANGFUSE_TRACING_ENABLED", "true")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", _unique_key("pk-test"))
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", _unique_key("sk-test"))
    monkeypatch.setenv("LANGFUSE_BASE_URL", "http://localhost:9999")
    monkeypatch.setenv("LANGFUSE_TRACING_ENVIRONMENT", "test")
    monkeypatch.setenv("LANGFUSE_RELEASE", "test-release")
    monkeypatch.setenv("LANGFUSE_MEDIA_UPLOAD_ENABLED", "false")

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    tracer_mod.init_trace_client(span_exporter=exporter, tracer_provider=provider)
    yield exporter
    tracer_mod.shutdown_trace_client()
    provider.shutdown()


def test_tracing_disabled_without_project_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MYCODE_TRACING", raising=False)
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")

    assert not tracing_enabled()


def test_tracing_respects_official_disable_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MYCODE_TRACING", "1")
    monkeypatch.setenv("LANGFUSE_TRACING_ENABLED", "false")

    assert not tracing_enabled()


def test_init_requires_langfuse_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MYCODE_TRACING", "1")
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)

    with pytest.raises(RuntimeError, match="LANGFUSE"):
        tracer_mod.init_trace_client()


def test_resolve_observation_type() -> None:
    assert resolve_observation_type("turn") == "chain"
    assert resolve_observation_type("model_call") == "generation"
    assert resolve_observation_type("tool_call") == "tool"
    assert resolve_observation_type("bg.task") == "tool"
    assert resolve_observation_type("sub_agent") == "agent"
    assert resolve_observation_type("permission.check") == "guardrail"
    assert resolve_observation_type("wiki.recall.semantic") == "retriever"
    assert resolve_observation_type("plan_mode.enter") == "event"
    assert resolve_observation_type("unknown") == "span"
    assert resolve_observation_type("unknown", default="event") == "event"


def test_trace_span_without_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MYCODE_TRACING", raising=False)

    with trace_span("model_call", name="model.test", model="gpt-test") as span:
        span.update(output="ok")
        span.add_metadata(job_id="job1")

        assert span.name == "model.test"
        assert span.observation_type == "generation"
        assert span.metadata == {"job_id": "job1"}
        assert span.get_trace_id() is None
        assert span.get_observation_id() is None

    trace_event("plan_mode.enter", metadata={"mode": "plan"})


def test_trace_context_and_domain_fields(langfuse_exporter: InMemorySpanExporter) -> None:
    with trace_context(
        session_id="session-1",
        trace_name="root-trace",
        tags=["main-agent"],
        metadata={
            "model": "gpt-test",
            "enabled": True,
            "non_ascii": "中文",
        },
    ):
        with trace_span("turn", input="hello", metadata={"turn_id": "turn-1"}) as turn:
            with trace_span(
                "model_call",
                name="model.test",
                model="gpt-test",
                model_parameters={"temperature": 0.1},
            ) as model_span:
                model_span.update(
                    output="ok",
                    usage_details={"input": 1, "output": 2, "total": 3},
                )

            with trace_span("tool_call", name="tool.read_file", input={"path": "a.py"}) as tool_span:
                tool_span.update(output="content")

            turn.update(output="done")

            trace_event("plan_mode.enter", metadata={"mode": "plan"})

            with trace_context(session_id="session-nested", trace_name="nested-trace"):
                with trace_span("compact", metadata={"tokens_before": 10}):
                    pass

    tracer_mod.flush_trace_client()
    spans = langfuse_exporter.get_finished_spans()

    turn_attrs = _attributes(spans, "turn")
    assert turn_attrs["session.id"] == "session-1"
    assert turn_attrs["langfuse.trace.name"] == "root-trace"
    assert list(turn_attrs["langfuse.trace.tags"]) == ["main-agent"]
    assert turn_attrs["langfuse.trace.metadata.model"] == "gpt-test"
    assert turn_attrs["langfuse.trace.metadata.enabled"] == "true"
    assert "langfuse.trace.metadata.non_ascii" not in turn_attrs
    assert turn_attrs["langfuse.environment"] == "test"
    assert turn_attrs["langfuse.release"] == "test-release"
    assert turn_attrs["langfuse.observation.type"] == "chain"
    assert turn_attrs["langfuse.observation.input"] == "hello"
    assert turn_attrs["langfuse.observation.output"] == "done"
    assert turn_attrs["langfuse.observation.metadata.turn_id"] == "turn-1"

    model_attrs = _attributes(spans, "model.test")
    assert model_attrs["langfuse.observation.type"] == "generation"
    assert model_attrs["langfuse.observation.model.name"] == "gpt-test"
    assert json.loads(model_attrs["langfuse.observation.model.parameters"]) == {"temperature": 0.1}
    assert json.loads(model_attrs["langfuse.observation.usage_details"]) == {
        "input": 1,
        "output": 2,
        "total": 3,
    }

    tool_attrs = _attributes(spans, "tool.read_file")
    assert tool_attrs["langfuse.observation.type"] == "tool"
    assert json.loads(tool_attrs["langfuse.observation.input"]) == {"path": "a.py"}
    assert tool_attrs["langfuse.observation.output"] == "content"

    event_attrs = _attributes(spans, "plan_mode.enter")
    assert event_attrs["langfuse.observation.type"] == "event"
    assert event_attrs["langfuse.observation.metadata.mode"] == "plan"

    compact_attrs = _attributes(spans, "compact")
    assert compact_attrs["langfuse.trace.name"] == "root-trace"
    assert compact_attrs["session.id"] == "session-1"
    assert compact_attrs["langfuse.observation.metadata.tokens_before"] == 10


def test_forced_trace_context_inside_active_span(langfuse_exporter: InMemorySpanExporter) -> None:
    with trace_context(session_id="outer-session", trace_name="outer-trace"):
        with trace_span("turn"):
            with trace_context(session_id="inner-session", trace_name="inner-trace", force=True):
                with trace_span("compact"):
                    pass

    tracer_mod.flush_trace_client()
    spans = langfuse_exporter.get_finished_spans()
    compact_attrs = _attributes(spans, "compact")

    assert compact_attrs["session.id"] == "inner-session"
    assert compact_attrs["langfuse.trace.name"] == "inner-trace"


def test_manual_span_ids_and_mapped_event(langfuse_exporter: InMemorySpanExporter) -> None:
    with trace_context(session_id="manual-session", trace_name="manual-trace"):
        span = start_trace_span("bg.task", name="bg.task.job", metadata={"job_id": "job"})
        trace_id = span.get_trace_id()
        observation_id = span.get_observation_id()
        span.end()

        trace_event("tool_call", name="tool.instant", output="ok")

    tracer_mod.flush_trace_client()
    spans = langfuse_exporter.get_finished_spans()
    manual_span = next(span for span in spans if span.name == "bg.task.job")
    instant_attrs = _attributes(spans, "tool.instant")

    assert trace_id == format(manual_span.context.trace_id, "032x")
    assert observation_id == format(manual_span.context.span_id, "016x")
    assert dict(manual_span.attributes or {})["langfuse.observation.type"] == "tool"
    assert dict(manual_span.attributes or {})["langfuse.observation.metadata.job_id"] == "job"
    assert instant_attrs["langfuse.observation.type"] == "tool"
    assert instant_attrs["langfuse.observation.output"] == "ok"


def test_trace_span_records_error(langfuse_exporter: InMemorySpanExporter) -> None:
    with trace_context(session_id="error-session", trace_name="error-trace"):
        with pytest.raises(RuntimeError, match="boom"):
            with trace_span("tool_call", name="tool.fail"):
                raise RuntimeError("boom")

    tracer_mod.flush_trace_client()
    attrs = _attributes(langfuse_exporter.get_finished_spans(), "tool.fail")

    assert attrs["langfuse.observation.level"] == "ERROR"
    assert attrs["langfuse.observation.status_message"] == "RuntimeError: boom"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
