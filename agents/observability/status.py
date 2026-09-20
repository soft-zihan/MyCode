"""Langfuse status discovery for the frontend API."""

from __future__ import annotations

import os
import time
from typing import Any

from agents.logging import print_error
from agents.observability.langfuse_api import LangfuseApiClient
from agents.observability.tracer import get_langfuse_base_url, tracing_enabled

_STATUS_TTL_SECONDS = 60

_cache: dict[str, Any] = {"expires_at": 0.0}
_client: LangfuseApiClient | None = None


def _get_status_client() -> LangfuseApiClient:
    global _client

    if _client is None:
        _client = LangfuseApiClient(timeout=5)
    return _client


def _public_status() -> dict[str, Any]:
    return {key: value for key, value in _cache.items() if key != "expires_at"}


def get_langfuse_status(force: bool = False) -> dict[str, Any]:
    now = time.time()
    if not force and _cache.get("expires_at", 0) > now:
        return _public_status()

    endpoint = get_langfuse_base_url()
    status: dict[str, Any] = {
        "endpoint": endpoint,
        "tracing_enabled": tracing_enabled(),
        "reachable": False,
        "project_id": None,
        "project_name": None,
        "error": None,
        "checked_at": now,
    }

    try:
        client = _get_status_client()
    except Exception as exc:
        status["error"] = f"{type(exc).__name__}: {exc}"
        _update_cache(status, now)
        return _public_status()

    try:
        health = client.api.health.health()
        status["reachable"] = str(getattr(health, "status", "OK")).upper() == "OK"
    except Exception as exc:
        status["error"] = f"{type(exc).__name__}: {exc}"
        print_error(f"[observability] Langfuse health check failed: {status['error']}")

    try:
        projects = client.api.projects.get().data or []
        preferred = os.environ.get("MYCODE_LANGFUSE_PROJECT_ID", "").strip()
        project = next(
            (item for item in projects if item.id == preferred),
            projects[0] if projects else None,
        )
        if project:
            status["project_id"] = project.id
            status["project_name"] = project.name
            status["trace_url_template"] = f"{endpoint}/project/{project.id}/traces/{{trace_id}}"
            status["session_url_template"] = f"{endpoint}/project/{project.id}/sessions/{{session_id}}"
        else:
            status["error"] = status["error"] or "Langfuse projects API returned no project"
    except Exception as exc:
        status["error"] = f"{type(exc).__name__}: {exc}"
        print_error(f"[observability] Langfuse project discovery failed: {status['error']}")

    _update_cache(status, now)
    return _public_status()


def _update_cache(status: dict[str, Any], now: float) -> None:
    _cache.clear()
    _cache.update(status)
    _cache["expires_at"] = now + _STATUS_TTL_SECONDS
