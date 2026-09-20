"""Domain trace API backed by the Langfuse SDK."""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any

from agents.logging import print_error
from agents.observability.tracer import (
    create_event,
    get_trace_client,
    start_current_observation,
    start_observation,
    trace_context,
    tracing_enabled,
)

__all__ = [
    "SPAN_TYPES",
    "TraceSpan",
    "resolve_observation_type",
    "start_trace_span",
    "trace_context",
    "trace_event",
    "trace_span",
    "tracing_enabled",
]

_UNSET: Any = object()

SPAN_TYPES: dict[str, str] = {
    "turn": "chain",
    "compact": "chain",
    "eval_task": "chain",
    "skill.extract": "chain",
    "skill.compile": "chain",
    "memory.maintenance": "chain",
    "wiki.write": "chain",
    "wiki.preflight": "chain",
    "plan_mode.enter": "event",
    "plan_mode.approved": "event",
    "plan_mode.rejected": "event",
    "model_call": "generation",
    "side_query": "generation",
    "tool_call": "tool",
    "tool": "tool",
    "bg.task": "tool",
    "skill.venv": "tool",
    "skill.install": "tool",
    "skill.inject": "tool",
    "skill.load_batch": "tool",
    "skill.write": "tool",
    "rule.write": "tool",
    "champion.activate": "tool",
    "memory.action": "tool",
    "mcp.init": "tool",
    "mcp.tool_call": "tool",
    "wiki.recall": "retriever",
    "wiki.recall.semantic": "retriever",
    "wiki.recall.hybrid": "retriever",
    "sub_agent": "agent",
    "permission.check": "guardrail",
}


def _log_trace_error(context: str, exc: BaseException) -> None:
    print_error(f"[trace] {context} failed: {type(exc).__name__}: {exc}")


def resolve_observation_type(kind: str, *, default: str = "span") -> str:
    prefix = kind.split(".")[0]
    return SPAN_TYPES.get(kind) or SPAN_TYPES.get(prefix) or default


class TraceSpan:
    def __init__(
        self,
        kind: str,
        name: str | None = None,
        *,
        input: Any = _UNSET,
        output: Any = _UNSET,
        metadata: Mapping[str, Any] | None = None,
        model: Any = _UNSET,
        model_parameters: Any = _UNSET,
        usage_details: Any = _UNSET,
        cost_details: Any = _UNSET,
        level: Any = _UNSET,
        status_message: Any = _UNSET,
        version: Any = _UNSET,
        completion_start_time: Any = _UNSET,
    ) -> None:
        self.kind = kind
        self.name = name or kind
        self.observation_type = resolve_observation_type(kind)
        self.metadata: dict[str, Any] = dict(metadata or {})
        self._fields: dict[str, Any] = {
            "input": input,
            "output": output,
            "model": model,
            "model_parameters": model_parameters,
            "usage_details": usage_details,
            "cost_details": cost_details,
            "level": level,
            "status_message": status_message,
            "version": version,
            "completion_start_time": completion_start_time,
        }
        self._context: Any | None = None
        self._observation: Any | None = None
        self._start_time: float | None = None

    def _payload(self) -> dict[str, Any]:
        payload = {
            key: value
            for key, value in self._fields.items()
            if value is not _UNSET and value is not None
        }
        if self.metadata:
            payload["metadata"] = dict(self.metadata)
        return payload

    def __enter__(self) -> "TraceSpan":
        self._start_time = time.time()
        if get_trace_client() is None:
            return self

        try:
            as_type = "span" if self.observation_type == "event" else self.observation_type
            self._context = start_current_observation(
                name=self.name,
                as_type=as_type,
                **self._payload(),
            )
            if self._context is not None:
                self._observation = self._context.__enter__()
        except Exception as exc:
            self._context = None
            self._observation = None
            _log_trace_error(f"start span {self.kind}", exc)
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> bool | None:
        if exc_val is not None:
            self.record_error(exc_val)

        context = self._context
        self._context = None
        self._observation = None
        if context is None:
            return False

        try:
            return context.__exit__(exc_type, exc_val, exc_tb)
        except Exception as exc:
            _log_trace_error(f"end span {self.kind}", exc)
            return False

    def start(self) -> "TraceSpan":
        self._start_time = time.time()
        if get_trace_client() is None:
            return self

        try:
            as_type = "span" if self.observation_type == "event" else self.observation_type
            self._observation = start_observation(
                name=self.name,
                as_type=as_type,
                **self._payload(),
            )
        except Exception as exc:
            self._observation = None
            _log_trace_error(f"start manual span {self.kind}", exc)
        return self

    def end(self) -> None:
        observation = self._observation
        self._observation = None
        if observation is None:
            return

        try:
            observation.end()
        except Exception as exc:
            _log_trace_error(f"end manual span {self.kind}", exc)

    def update(
        self,
        *,
        input: Any = _UNSET,
        output: Any = _UNSET,
        metadata: Mapping[str, Any] | None = _UNSET,
        model: Any = _UNSET,
        model_parameters: Any = _UNSET,
        usage_details: Any = _UNSET,
        cost_details: Any = _UNSET,
        level: Any = _UNSET,
        status_message: Any = _UNSET,
        version: Any = _UNSET,
        completion_start_time: Any = _UNSET,
    ) -> None:
        provided: dict[str, Any] = {}
        local_fields = {
            "input": input,
            "output": output,
            "model": model,
            "model_parameters": model_parameters,
            "usage_details": usage_details,
            "cost_details": cost_details,
            "level": level,
            "status_message": status_message,
            "version": version,
            "completion_start_time": completion_start_time,
        }

        for key, value in local_fields.items():
            if value is _UNSET:
                continue
            self._fields[key] = value
            if value is not None:
                provided[key] = value

        if metadata is not _UNSET and metadata:
            merged = dict(metadata)
            self.metadata.update(merged)
            provided["metadata"] = merged

        observation = self._observation
        if observation is None or not provided:
            return

        try:
            observation.update(**provided)
        except Exception as exc:
            _log_trace_error(f"update span {self.kind}", exc)

    def add_metadata(self, **metadata: Any) -> None:
        self.update(metadata=metadata)

    def set_metadata(self, key: str, value: Any) -> None:
        self.update(metadata={key: value})

    def record_error(self, error: BaseException) -> None:
        self.update(
            level="ERROR",
            status_message=f"{type(error).__name__}: {error}"[:500],
        )

    def get_trace_id(self) -> str | None:
        observation = self._observation
        if observation is None:
            return None

        try:
            trace_id = getattr(observation, "trace_id", None)
            if isinstance(trace_id, str) and trace_id.strip("0"):
                return trace_id
        except Exception as exc:
            _log_trace_error(f"read trace id for {self.kind}", exc)
        return None

    def get_observation_id(self) -> str | None:
        observation = self._observation
        if observation is None:
            return None

        try:
            observation_id = getattr(observation, "id", None)
            if isinstance(observation_id, str) and observation_id.strip("0"):
                return observation_id
        except Exception as exc:
            _log_trace_error(f"read observation id for {self.kind}", exc)
        return None


def trace_span(kind: str, name: str | None = None, **fields: Any) -> TraceSpan:
    return TraceSpan(kind, name=name, **fields)


def start_trace_span(kind: str, name: str | None = None, **fields: Any) -> TraceSpan:
    return TraceSpan(kind, name=name, **fields).start()


def trace_event(
    kind: str,
    name: str | None = None,
    *,
    input: Any = None,
    output: Any = None,
    metadata: Mapping[str, Any] | None = None,
    level: str | None = None,
    status_message: str | None = None,
    version: str | None = None,
) -> None:
    if get_trace_client() is None:
        return

    event_name = name or kind
    observation_type = resolve_observation_type(kind, default="event")
    fields = {
        "input": input,
        "output": output,
        "metadata": dict(metadata or {}),
        "level": level,
        "status_message": status_message,
        "version": version,
    }

    try:
        if observation_type == "event":
            create_event(name=event_name, **fields)
            return

        observation = start_observation(name=event_name, as_type=observation_type, **fields)
        if observation is not None:
            observation.end()
    except Exception as exc:
        _log_trace_error(f"emit event {kind}", exc)
