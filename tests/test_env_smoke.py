"""Step 0 smoke tests: env resolution and package import health."""

from __future__ import annotations

import pytest

from agents.main import _resolve_model, _resolve_api_config


def test_model_priority_cli_wins(monkeypatch):
    monkeypatch.setenv("MODEL", "env-model")
    assert _resolve_model("cli-model") == "cli-model"


def test_model_priority_model_env(monkeypatch):
    monkeypatch.setenv("MODEL", "env-model")
    assert _resolve_model(None) == "env-model"


def test_model_default(monkeypatch):
    assert _resolve_model(None) == "deepseek-chat"


def test_model_blank_values_ignored(monkeypatch):
    """Whitespace-only env values must not win over the default."""
    monkeypatch.setenv("MODEL", "   ")
    assert _resolve_model(None) == "deepseek-chat"


def test_resolve_api_config_openai(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://example.com/v1")
    base, key = _resolve_api_config(None)
    assert base == "https://example.com/v1"
    assert key == "sk-test"


def test_resolve_api_config_generic_endpoint(monkeypatch):
    """APIKEY / API drive any OpenAI-compatible endpoint."""
    monkeypatch.setenv("APIKEY", "sk-test")
    monkeypatch.setenv("API", "https://gateway.example.com/v1")
    base, key = _resolve_api_config(None)
    assert base == "https://gateway.example.com/v1"
    assert key == "sk-test"


def test_package_imports():
    import agents.agent  # noqa: F401
    import agents.tools  # noqa: F401
    import agents.core.session  # noqa: F401
    import agents.core.subagent  # noqa: F401
