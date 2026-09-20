"""Langfuse SDK tracer adapter."""

from __future__ import annotations

import getpass
import json
import os
import re
from contextlib import contextmanager
from typing import Any, Iterator

from agents.version import __version__

_client: Any | None = None

_DEFAULT_BASE_URL = "https://cloud.langfuse.com"
_INVALID_ENVIRONMENT_CHARS = re.compile(r"[^a-z0-9_-]+")
_FALSE_VALUES = ("", "0", "false", "no", "off")
_TRUE_VALUES = ("1", "true", "yes", "on")

_OBSERVATION_FIELDS = (
    "input",
    "output",
    "metadata",
    "model",
    "model_parameters",
    "usage_details",
    "cost_details",
    "level",
    "status_message",
    "version",
    "completion_start_time",
)


def _env_flag(name: str, default: str = "") -> bool:
    return os.environ.get(name, default).strip().lower() in _TRUE_VALUES


def tracing_enabled() -> bool:
    if os.environ.get("LANGFUSE_TRACING_ENABLED", "true").strip().lower() == "false":
        return False
    return _env_flag("MYCODE_TRACING")


def get_langfuse_base_url() -> str:
    return (os.environ.get("LANGFUSE_BASE_URL", "").strip() or _DEFAULT_BASE_URL).rstrip("/")


def get_trace_client() -> Any | None:
    return _client if tracing_enabled() else None


def _environment() -> str | None:
    raw = (
        os.environ.get("LANGFUSE_TRACING_ENVIRONMENT", "").strip()
        or os.environ.get("ENVIRONMENT", "").strip()
        or "development"
    )
    value = _INVALID_ENVIRONMENT_CHARS.sub("-", raw.lower()).strip("-_")[:40]
    if not value or value.startswith("langfuse"):
        return None
    return value


def _release() -> str:
    return os.environ.get("LANGFUSE_RELEASE", "").strip() or __version__


def _user_id() -> str | None:
    raw = os.environ.get("LANGFUSE_USER_ID", "").strip() or getpass.getuser()
    return _propagated_string(raw)


def _propagated_string(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        text = "true" if value else "false"
    elif isinstance(value, str):
        text = value.strip()
    elif isinstance(value, (int, float)):
        text = str(value)
    else:
        text = json.dumps(value, ensure_ascii=False, default=str)
    if not text or not text.isascii():
        return None
    return text[:200]


def _propagated_tags(tags: list[str] | tuple[str, ...] | None) -> list[str] | None:
    if not tags:
        return None
    sanitized: list[str] = []
    for tag in tags:
        text = _propagated_string(tag)
        if text and text not in sanitized:
            sanitized.append(text)
    return sanitized or None


def _propagated_metadata(metadata: dict[str, Any] | None) -> dict[str, str] | None:
    if not metadata:
        return None
    sanitized: dict[str, str] = {}
    for key, value in metadata.items():
        key_text = _propagated_string(key)
        value_text = _propagated_string(value)
        if key_text and value_text is not None:
            sanitized[key_text] = value_text
    return sanitized or None


def _has_active_span() -> bool:
    try:
        from opentelemetry import trace

        span_context = trace.get_current_span().get_span_context()
        return bool(span_context and span_context.is_valid)
    except Exception:
        return False


@contextmanager
def trace_context(
    *,
    session_id: str | None = None,
    trace_name: str | None = None,
    tags: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
    user_id: str | None = None,
    version: str | None = None,
    force: bool = False,
) -> Iterator[None]:
    client = get_trace_client()
    if client is None or (_has_active_span() and not force):
        yield
        return

    from langfuse import propagate_attributes

    environment = _environment()
    propagated_version = _propagated_string(version)
    propagated_user = _propagated_string(user_id if user_id is not None else _user_id())
    attributes: dict[str, Any] = {
        "session_id": _propagated_string(session_id),
        "trace_name": _propagated_string(trace_name),
        "tags": _propagated_tags(tags),
        "metadata": _propagated_metadata(metadata),
        "user_id": propagated_user,
        "version": propagated_version,
        "environment": environment,
    }
    kwargs = {key: value for key, value in attributes.items() if value}

    if not kwargs:
        yield
        return

    with propagate_attributes(**kwargs):
        yield


def _observation_kwargs(fields: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in fields.items() if key in _OBSERVATION_FIELDS and value is not None}


def start_current_observation(*, name: str, as_type: str = "span", **fields: Any) -> Any | None:
    client = get_trace_client()
    if client is None:
        return None
    return client.start_as_current_observation(name=name, as_type=as_type, **_observation_kwargs(fields))


def start_observation(*, name: str, as_type: str = "span", **fields: Any) -> Any | None:
    client = get_trace_client()
    if client is None:
        return None
    return client.start_observation(name=name, as_type=as_type, **_observation_kwargs(fields))


def create_event(*, name: str, **fields: Any) -> Any | None:
    client = get_trace_client()
    if client is None:
        return None
    return client.create_event(name=name, **_observation_kwargs(fields))


def init_trace_client(*, span_exporter: Any | None = None, tracer_provider: Any | None = None) -> None:
    global _client

    if _client is not None or not tracing_enabled():
        return

    public_key = os.environ.get("LANGFUSE_PUBLIC_KEY", "").strip()
    secret_key = os.environ.get("LANGFUSE_SECRET_KEY", "").strip()
    if not public_key or not secret_key:
        raise RuntimeError(
            "LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY 未配置，无法初始化 Langfuse tracing（请检查 .env）"
        )

    from langfuse import Langfuse

    _client = Langfuse(
        public_key=public_key,
        secret_key=secret_key,
        base_url=get_langfuse_base_url(),
        environment=_environment(),
        release=_release(),
        tracing_enabled=True,
        additional_headers={"x-langfuse-ingestion-version": "4"},
        span_exporter=span_exporter,
        tracer_provider=tracer_provider,
    )


def flush_trace_client() -> None:
    if _client is not None:
        _client.flush()


def shutdown_trace_client() -> None:
    global _client
    if _client is not None:
        _client.shutdown()
        _client = None
