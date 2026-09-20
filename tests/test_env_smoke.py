"""Step 0 smoke tests: env resolution and package import health."""

from __future__ import annotations

import pytest

from agents.main import _resolve_model, _resolve_api_config


def test_model_priority_cli_wins(monkeypatch):
    monkeypatch.setenv("MODEL", "env-model")
    monkeypatch.setenv("MINI_CLAUDE_MODEL", "mini-model")
    assert _resolve_model("cli-model") == "cli-model"


def test_model_priority_model_env(monkeypatch):
    monkeypatch.setenv("MODEL", "env-model")
    monkeypatch.setenv("MINI_CLAUDE_MODEL", "mini-model")
    assert _resolve_model(None) == "env-model"


def test_model_priority_mini_claude_fallback(monkeypatch):
    # .env.example documents MINI_CLAUDE_MODEL; it must be honored when MODEL is absent.
    monkeypatch.setenv("MINI_CLAUDE_MODEL", "deepseek-v4-flash")
    assert _resolve_model(None) == "deepseek-v4-flash"


def test_model_default(monkeypatch):
    assert _resolve_model(None) == "deepseek-chat"


def test_model_blank_values_ignored(monkeypatch):
    monkeypatch.setenv("MODEL", "   ")
    monkeypatch.setenv("MINI_CLAUDE_MODEL", "real-model")
    assert _resolve_model(None) == "real-model"


def test_resolve_api_config_openai(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://example.com/v1")
    base, key = _resolve_api_config(None)
    assert base == "https://example.com/v1"
    assert key == "sk-test"


def test_resolve_api_config_anthropic_compatible(monkeypatch):
    monkeypatch.setenv("APIKEY", "sk-test")
    monkeypatch.setenv("API", "https://example.com/anthropic")
    base, key = _resolve_api_config(None)
    assert base == "https://example.com/anthropic"
    assert key == "sk-test"


def test_package_imports():
    import agents.agent  # noqa: F401
    import agents.tools  # noqa: F401
    import agents.memory  # noqa: F401
    import agents.core.session  # noqa: F401
    import agents.core.subagent  # noqa: F401
