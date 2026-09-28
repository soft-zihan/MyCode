"""BC-30：embedding 后端全局配置化——OpenAI 兼容 API 分发、缓存路径消毒、模型切换。"""

from __future__ import annotations

import pytest

from agents.config import EmbeddingConfig


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    from agents.wiki.evolution.embedding import EmbeddingCache
    EmbeddingCache._instance = None
    yield tmp_path
    EmbeddingCache._instance = None


def test_embedding_config_defaults():
    cfg = EmbeddingConfig()
    assert cfg.backend == "openai"
    assert cfg.model == "BAAI/bge-large-zh-v1.5"
    assert cfg.base_url == "https://api.siliconflow.cn/v1"


def test_embedding_config_roundtrip():
    cfg = EmbeddingConfig(backend="ollama", base_url="", api_key="k", model="qwen3-embedding")
    assert EmbeddingConfig.from_dict(cfg.to_dict()) == cfg
    # 缺省段容忍（旧 config.json 无 embedding 键）
    assert EmbeddingConfig.from_dict({}).backend == "openai"


def test_cache_path_sanitizes_model_name(home):
    from agents.wiki.evolution.embedding import get_cache_path

    p = get_cache_path("BAAI/bge-large-zh-v1.5")
    assert p.name == "BAAI_bge-large-zh-v1.5.json"
    assert p.parent == home / ".mycode" / "embed-cache"


@pytest.mark.asyncio
async def test_embed_text_openai_backend_and_cache(home, monkeypatch):
    import agents.config as config_mod
    from agents.wiki.evolution import embedding as emb

    monkeypatch.setattr(emb, "get_embedding_config",
                        lambda: EmbeddingConfig(api_key="test-key"))

    calls = []

    async def fake_openai(text, model):
        calls.append((text, model))
        return [0.1, 0.2, 0.3]

    monkeypatch.setattr(emb, "_call_openai_embedding", fake_openai)

    v1 = await emb.embed_text("连接池配置")
    assert v1 == [0.1, 0.2, 0.3]
    assert calls == [("连接池配置", "BAAI/bge-large-zh-v1.5")]

    v2 = await emb.embed_text("连接池配置")  # 缓存命中，不再调 API
    assert v2 == v1
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_embed_text_ollama_backend_routes(home, monkeypatch):
    import agents.config as config_mod
    from agents.wiki.evolution import embedding as emb

    monkeypatch.setattr(emb, "get_embedding_config",
                        lambda: EmbeddingConfig(backend="ollama", model="qwen3-embedding"))

    called = []

    async def fake_ollama(text, model):
        called.append(model)
        return [0.5] * 4

    monkeypatch.setattr(emb, "_call_ollama_embedding", fake_ollama)
    v = await emb.embed_text("test")
    assert v == [0.5] * 4
    assert called == ["qwen3-embedding"]


@pytest.mark.asyncio
async def test_embed_text_unsupported_backend_raises(home, monkeypatch):
    import agents.config as config_mod
    from agents.wiki.evolution import embedding as emb

    monkeypatch.setattr(emb, "get_embedding_config",
                        lambda: EmbeddingConfig(backend="nope"))
    with pytest.raises(RuntimeError, match="unsupported embedding backend"):
        await emb.embed_text("test")
