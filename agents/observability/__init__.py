"""MyCode observability."""

from __future__ import annotations

from agents.logging import print_error


def init_tracing() -> None:
    try:
        from .tracer import init_trace_client

        init_trace_client()
    except Exception as exc:
        print_error(f"[observability] Langfuse tracing initialization failed: {type(exc).__name__}: {exc}")


def flush_tracing() -> None:
    try:
        from .tracer import flush_trace_client

        flush_trace_client()
    except Exception as exc:
        print_error(f"[observability] Langfuse tracing flush failed: {type(exc).__name__}: {exc}")


def shutdown_tracing() -> None:
    try:
        from .tracer import shutdown_trace_client

        shutdown_trace_client()
    except Exception as exc:
        print_error(f"[observability] Langfuse tracing shutdown failed: {type(exc).__name__}: {exc}")
