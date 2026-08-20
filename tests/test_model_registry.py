"""Step 3: model registry — independent endpoints + per-agent routing.

Design:
- Endpoints are registered via BEAR_ENDPOINT_<ID>_BASE_URL / _API_KEY / _MODEL
  (each endpoint fully independent: own url, key, model).
- Sub-agent routing: BEAR_MODEL_<TYPE> or custom-agent frontmatter `model:`.
  The value is either an endpoint ID (full endpoint config) or a bare model
  name (primary endpoint's url/key with only the model swapped).
- BEAR_SIDE_MODEL routes side queries (memory recall, folding, skill evolution).
"""

from __future__ import annotations

from agents.model_registry import (
    ModelEndpoint,
    discover_endpoints,
    resolve_agent_endpoint,
    resolve_side_endpoint,
    get_agent_model_ref,
)


PRIMARY = ModelEndpoint(
    model="deepseek-v4-pro",
    base_url="https://main.example.com/v1",
    api_key="sk-main",
    use_openai=True,
)


# ── endpoint discovery ──────────────────────────────────────


def test_discover_endpoints_parses_env(monkeypatch):
    monkeypatch.setenv("BEAR_ENDPOINT_CHEAP_BASE_URL", "https://cheap.example.com/v1")
    monkeypatch.setenv("BEAR_ENDPOINT_CHEAP_API_KEY", "sk-cheap")
    monkeypatch.setenv("BEAR_ENDPOINT_CHEAP_MODEL", "deepseek-v4-flash")
    eps = discover_endpoints()
    assert "cheap" in eps
    assert eps["cheap"].model == "deepseek-v4-flash"
    assert eps["cheap"].base_url == "https://cheap.example.com/v1"
    assert eps["cheap"].api_key == "sk-cheap"
    assert eps["cheap"].use_openai is True


def test_discover_endpoints_anthropic_protocol(monkeypatch):
    monkeypatch.setenv("BEAR_ENDPOINT_CLAUDE_BASE_URL", "https://host.example.com/anthropic")
    monkeypatch.setenv("BEAR_ENDPOINT_CLAUDE_API_KEY", "sk-ant")
    monkeypatch.setenv("BEAR_ENDPOINT_CLAUDE_MODEL", "claude-sonnet-4-6")
    eps = discover_endpoints()
    assert eps["claude"].use_openai is False


def test_discover_endpoints_requires_base_url(monkeypatch):
    # An endpoint without BASE_URL is incomplete and must be skipped.
    monkeypatch.setenv("BEAR_ENDPOINT_BROKEN_API_KEY", "sk-x")
    monkeypatch.setenv("BEAR_ENDPOINT_BROKEN_MODEL", "m")
    assert discover_endpoints() == {}


def test_discover_endpoints_defaults_model_from_id(monkeypatch):
    # MODEL optional: fall back to empty -> caller keeps primary model.
    monkeypatch.setenv("BEAR_ENDPOINT_ALT_BASE_URL", "https://alt.example.com/v1")
    monkeypatch.setenv("BEAR_ENDPOINT_ALT_API_KEY", "sk-alt")
    eps = discover_endpoints()
    assert eps["alt"].model == ""


# ── routing resolution ──────────────────────────────────────


def test_ref_model_name_reverse_lookup(monkeypatch):
    """核心用法：子 Agent 写模型名，注册表按模型名反查端点并使用其 url/key。"""
    monkeypatch.setenv("BEAR_ENDPOINT_XYZ_BASE_URL", "https://other-vendor.example.com/v1")
    monkeypatch.setenv("BEAR_ENDPOINT_XYZ_API_KEY", "sk-other")
    monkeypatch.setenv("BEAR_ENDPOINT_XYZ_MODEL", "deepseek-v4-flash")
    ep = resolve_agent_endpoint("explore", model_ref="deepseek-v4-flash", primary=PRIMARY)
    assert ep.model == "deepseek-v4-flash"
    assert ep.base_url == "https://other-vendor.example.com/v1"
    assert ep.api_key == "sk-other"


def test_ref_model_name_case_insensitive(monkeypatch):
    monkeypatch.setenv("BEAR_ENDPOINT_XYZ_BASE_URL", "https://other.example.com/v1")
    monkeypatch.setenv("BEAR_ENDPOINT_XYZ_API_KEY", "sk-other")
    monkeypatch.setenv("BEAR_ENDPOINT_XYZ_MODEL", "Qwen3.8-Max")
    ep = resolve_agent_endpoint("explore", model_ref="qwen3.8-max", primary=PRIMARY)
    assert ep.base_url == "https://other.example.com/v1"


def test_ref_endpoint_id_compat(monkeypatch):
    """兼容写法：直接写端点 ID 也能命中。"""
    monkeypatch.setenv("BEAR_ENDPOINT_CHEAP_BASE_URL", "https://cheap.example.com/v1")
    monkeypatch.setenv("BEAR_ENDPOINT_CHEAP_API_KEY", "sk-cheap")
    monkeypatch.setenv("BEAR_ENDPOINT_CHEAP_MODEL", "deepseek-v4-flash")
    ep = resolve_agent_endpoint("explore", model_ref="cheap", primary=PRIMARY)
    assert ep.model == "deepseek-v4-flash"
    assert ep.base_url == "https://cheap.example.com/v1"
    assert ep.api_key == "sk-cheap"


def test_ref_unregistered_model_uses_primary_connection(monkeypatch):
    """未注册的模型名：沿用主端点 url/key 仅换模型名（同 API 多模型场景）。"""
    ep = resolve_agent_endpoint("explore", model_ref="some-new-model", primary=PRIMARY)
    assert ep.model == "some-new-model"
    assert ep.base_url == PRIMARY.base_url
    assert ep.api_key == PRIMARY.api_key


def test_ref_empty_inherits_primary(monkeypatch):
    ep = resolve_agent_endpoint("explore", model_ref="", primary=PRIMARY)
    assert ep == PRIMARY


def test_adding_new_api_endpoint(monkeypatch):
    """用户可以随时添加新的 API：再加一组 BEAR_ENDPOINT_<ID>_* 即可被路由。"""
    # 已有两个端点
    monkeypatch.setenv("BEAR_ENDPOINT_A_BASE_URL", "https://a.example.com/v1")
    monkeypatch.setenv("BEAR_ENDPOINT_A_API_KEY", "sk-a")
    monkeypatch.setenv("BEAR_ENDPOINT_A_MODEL", "model-a")
    monkeypatch.setenv("BEAR_ENDPOINT_B_BASE_URL", "https://b.example.com/v1")
    monkeypatch.setenv("BEAR_ENDPOINT_B_API_KEY", "sk-b")
    monkeypatch.setenv("BEAR_ENDPOINT_B_MODEL", "model-b")
    # 用户新增第三个 API（不同厂商）
    monkeypatch.setenv("BEAR_ENDPOINT_C_BASE_URL", "https://c.example.com/anthropic")
    monkeypatch.setenv("BEAR_ENDPOINT_C_API_KEY", "sk-c")
    monkeypatch.setenv("BEAR_ENDPOINT_C_MODEL", "claude-sonnet-4-6")

    eps = discover_endpoints()
    assert set(eps) == {"a", "b", "c"}
    assert eps["c"].use_openai is False  # /anthropic 路径自动识别协议

    # 按模型名路由到新端点
    ep = resolve_agent_endpoint("plan", model_ref="claude-sonnet-4-6", primary=PRIMARY)
    assert ep.base_url == "https://c.example.com/anthropic"
    assert ep.api_key == "sk-c"
    assert ep.use_openai is False


def test_env_model_type_is_read(monkeypatch):
    monkeypatch.setenv("BEAR_ENDPOINT_CHEAP_BASE_URL", "https://cheap.example.com/v1")
    monkeypatch.setenv("BEAR_ENDPOINT_CHEAP_API_KEY", "sk-cheap")
    monkeypatch.setenv("BEAR_ENDPOINT_CHEAP_MODEL", "deepseek-v4-flash")
    monkeypatch.setenv("BEAR_MODEL_EXPLORE", "deepseek-v4-flash")
    ref = get_agent_model_ref("explore")
    assert ref == "deepseek-v4-flash"
    ep = resolve_agent_endpoint("explore", model_ref=ref, primary=PRIMARY)
    assert ep.model == "deepseek-v4-flash"
    assert ep.base_url == "https://cheap.example.com/v1"


def test_env_model_type_uppercases_and_sanitizes(monkeypatch):
    monkeypatch.setenv("BEAR_MODEL_MY_AGENT", "deepseek-v4-flash")
    assert get_agent_model_ref("my-agent") == "deepseek-v4-flash"


def test_side_endpoint_routing(monkeypatch):
    monkeypatch.setenv("BEAR_ENDPOINT_CHEAP_BASE_URL", "https://cheap.example.com/v1")
    monkeypatch.setenv("BEAR_ENDPOINT_CHEAP_API_KEY", "sk-cheap")
    monkeypatch.setenv("BEAR_ENDPOINT_CHEAP_MODEL", "deepseek-v4-flash")
    monkeypatch.setenv("BEAR_SIDE_MODEL", "deepseek-v4-flash")
    ep = resolve_side_endpoint(primary=PRIMARY)
    assert ep.model == "deepseek-v4-flash"
    assert ep.base_url == "https://cheap.example.com/v1"


def test_side_endpoint_defaults_to_primary(monkeypatch):
    ep = resolve_side_endpoint(primary=PRIMARY)
    assert ep == PRIMARY
