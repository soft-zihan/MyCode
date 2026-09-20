"""Step 3 integration: Agent._spawn_sub_agent routes via the model registry."""

from __future__ import annotations

from agents.agent import Agent


def _make_parent(**kwargs):
    defaults = dict(model="deepseek-v4-pro", api_base="https://main.example.com/v1", api_key="sk-main")
    defaults.update(kwargs)
    return Agent(**defaults)


def test_spawn_sub_agent_inherits_primary_when_no_ref(monkeypatch):
    parent = _make_parent()
    child = parent._spawn_sub_agent(system_prompt="sp", tools=[], model_ref="", label="explore")
    assert child.model == "deepseek-v4-pro"
    assert child._api_base == "https://main.example.com/v1"
    assert child._api_key == "sk-main"
    assert child.is_sub_agent is True


def test_spawn_sub_agent_env_endpoint_id(monkeypatch):
    monkeypatch.setenv("MYCODE_ENDPOINT_CHEAP_BASE_URL", "https://cheap.example.com/v1")
    monkeypatch.setenv("MYCODE_ENDPOINT_CHEAP_API_KEY", "sk-cheap")
    monkeypatch.setenv("MYCODE_ENDPOINT_CHEAP_MODEL", "deepseek-v4-flash")
    parent = _make_parent()
    # 按模型名选择 → 注册表反查到 cheap 端点，使用其 url/key
    child = parent._spawn_sub_agent(system_prompt="sp", tools=[], model_ref="deepseek-v4-flash", label="explore")
    assert child.model == "deepseek-v4-flash"
    assert child._api_base == "https://cheap.example.com/v1"
    assert child._api_key == "sk-cheap"


def test_spawn_sub_agent_bare_model_name(monkeypatch):
    parent = _make_parent()
    # 未注册的模型名 → 沿用主端点 url/key 仅换模型名
    child = parent._spawn_sub_agent(system_prompt="sp", tools=[], model_ref="deepseek-v4-flash", label="plan")
    assert child.model == "deepseek-v4-flash"
    assert child._api_base == "https://main.example.com/v1"
    assert child._api_key == "sk-main"


def test_sub_agent_config_provides_model_ref(monkeypatch):
    from agents.core.subagent import get_sub_agent_config

    monkeypatch.setenv("MYCODE_MODEL_EXPLORE", "qwen3.8-max")
    cfg = get_sub_agent_config("explore")
    assert cfg["model_ref"] == "qwen3.8-max"
    # general with no env override -> empty ref (inherit)
    cfg_general = get_sub_agent_config("general")
    assert cfg_general["model_ref"] == ""


def test_custom_agent_frontmatter_model(monkeypatch, tmp_path):
    """自定义子 Agent 通过 frontmatter model: 字段自行配置模型，无需改代码。"""
    import agents.core.subagent as subagent_mod

    agents_dir = tmp_path / ".mycode" / "agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "reviewer.md").write_text(
        "---\n"
        "name: reviewer\n"
        "description: code reviewer\n"
        "model: qwen3.8-max\n"
        "---\n"
        "You are a code reviewer.\n"
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(subagent_mod, "_custom_agents_cache", {})
    try:
        cfg = subagent_mod.get_sub_agent_config("reviewer")
        assert cfg["model_ref"] == "qwen3.8-max"
    finally:
        monkeypatch.setattr(subagent_mod, "_custom_agents_cache", {})


def test_side_query_uses_side_endpoint(monkeypatch):
    monkeypatch.setenv("MYCODE_ENDPOINT_CHEAP_BASE_URL", "https://cheap.example.com/v1")
    monkeypatch.setenv("MYCODE_ENDPOINT_CHEAP_API_KEY", "sk-cheap")
    monkeypatch.setenv("MYCODE_ENDPOINT_CHEAP_MODEL", "deepseek-v4-flash")
    monkeypatch.setenv("MYCODE_SIDE_MODEL", "deepseek-v4-flash")
    parent = _make_parent()
    side = parent._get_side_client()
    assert side is not None
    client, model, use_openai = side
    assert model == "deepseek-v4-flash"
    assert use_openai is True
    # _build_side_query should return a callable (the OpenAI path)
    sq = parent._build_side_query()
    assert callable(sq)


def test_side_query_defaults_to_main_client(monkeypatch):
    parent = _make_parent()
    assert parent._get_side_client() is None
    sq = parent._build_side_query()
    assert callable(sq)
