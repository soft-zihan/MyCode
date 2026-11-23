"""Step 0 smoke tests: env resolution and package import health."""

from __future__ import annotations

import json

import pytest

from agents.main import _resolve_model, _resolve_api_config, _resolve_runtime_config


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


@pytest.fixture
def config_file(tmp_path, monkeypatch):
    """把 config.json 指到临时文件，并清掉四个凭据 env。"""
    for var in ("APIKEY", "API", "OPENAI_API_KEY", "OPENAI_BASE_URL", "MODEL"):
        monkeypatch.delenv(var, raising=False)
    path = tmp_path / "config.json"
    monkeypatch.setenv("MYCODE_CONFIG_PATH", str(path))

    def write(endpoints: dict, routing: dict | None = None) -> None:
        path.write_text(
            json.dumps({"endpoints": endpoints, "routing": routing or {}}),
            encoding="utf-8",
        )

    return write


def test_runtime_config_falls_back_to_config_json(config_file):
    """沙箱 Job 只挂得到 config.json，没有 .env：CLI 必须认这份配置。"""
    config_file(
        {"m1": {"model": "kimi-k2", "base_url": "https://gw.example.com/v1", "api_key": "sk-cfg"}},
        {"primary": "m1"},
    )
    model, base, key = _resolve_runtime_config(None, None)
    assert (model, base, key) == ("kimi-k2", "https://gw.example.com/v1", "sk-cfg")


def test_runtime_config_env_wins_over_config_json(config_file, monkeypatch):
    config_file(
        {"m1": {"model": "kimi-k2", "base_url": "https://gw.example.com/v1", "api_key": "sk-cfg"}},
        {"primary": "m1"},
    )
    monkeypatch.setenv("APIKEY", "sk-env")
    monkeypatch.setenv("API", "https://env.example.com/v1")
    monkeypatch.setenv("MODEL", "env-model")
    model, base, key = _resolve_runtime_config(None, None)
    assert (model, base, key) == ("env-model", "https://env.example.com/v1", "sk-env")


def test_runtime_config_model_env_selects_matching_endpoint(config_file, monkeypatch):
    config_file(
        {
            "m1": {"model": "kimi-k2", "base_url": "https://one.example.com/v1", "api_key": "sk-one"},
            "m2": {"model": "glm-5", "base_url": "https://two.example.com/v1", "api_key": "sk-two"},
        },
        {"primary": "m1"},
    )
    monkeypatch.setenv("MODEL", "glm-5")
    model, base, key = _resolve_runtime_config(None, None)
    assert (model, base, key) == ("glm-5", "https://two.example.com/v1", "sk-two")


def test_runtime_config_cli_api_base_wins_over_config_json(config_file):
    config_file(
        {"m1": {"model": "kimi-k2", "base_url": "https://gw.example.com/v1", "api_key": "sk-cfg"}},
        {"primary": "m1"},
    )
    _, base, key = _resolve_runtime_config(None, "https://cli.example.com/v1")
    assert base == "https://cli.example.com/v1"
    assert key == "sk-cfg"


def test_runtime_config_no_source_still_yields_no_key(config_file):
    """两边都没有就仍然返回空 key——启动处那条报错不能被悄悄绕过。"""
    config_file({})
    model, base, key = _resolve_runtime_config(None, None)
    assert key is None
    assert base is None
    assert model == "deepseek-chat"


def test_runtime_config_skips_endpoint_without_key(config_file):
    config_file(
        {"m1": {"model": "kimi-k2", "base_url": "https://gw.example.com/v1", "api_key": ""}},
        {"primary": "m1"},
    )
    _, _, key = _resolve_runtime_config(None, None)
    assert key is None


def test_package_imports():
    import agents.agent  # noqa: F401
    import agents.tools  # noqa: F401
    import agents.core.session  # noqa: F401
    import agents.core.subagent  # noqa: F401
