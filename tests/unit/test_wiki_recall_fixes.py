"""回归：B7 误删 content_hash 导致 hybrid_recall 全线 NameError（2026-09-21 eval bad case）。

单测盲区教训：embedding 调用全被 mock 时，模块内真实函数引用断裂测不出来。
这里直接走 EmbeddingCache 真实代码路径（不联网）。
"""

from __future__ import annotations

import pytest

from agents.core.workspace import workspace_scope
from agents.wiki.evolution.embedding import EmbeddingCache, content_hash
from agents.wiki.wiki_manager import WikiEntry, _keyword_fallback_select


@pytest.fixture
def ws(tmp_path, monkeypatch):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path))
    with workspace_scope(workspace):
        yield workspace


def test_embedding_cache_roundtrip_no_network(ws):
    cache = EmbeddingCache.get("test-model")
    assert cache.get_embedding("hello") is None
    cache.set_embedding("hello", [0.1, 0.2])
    assert cache.get_embedding("hello") == [0.1, 0.2]
    cache.save()
    cache2 = EmbeddingCache("test-model")
    cache2._load()
    assert cache2.get_embedding("hello") == [0.1, 0.2]


def test_content_hash_stable():
    assert content_hash("abc") == content_hash("abc")
    assert content_hash("abc") != content_hash("abd")


def _entry(name: str, rel: str, desc: str, content: str) -> WikiEntry:
    return WikiEntry(name=name, type="knowledge", filename=name + ".md",
                     rel_path=rel, content=content, meta={"description": desc})


def test_keyword_fallback_select():
    cands = [
        _entry("连接池配置", "knowledge/a.md", "数据库连接池", "POOL_SIZE 默认 10"),
        _entry("无关条目", "knowledge/b.md", "编辑器主题", "dark mode"),
    ]
    got = _keyword_fallback_select("数据库连接池怎么配置", cands)
    assert [e.rel_path for e in got] == ["knowledge/a.md"]


def test_keyword_fallback_no_match_returns_empty():
    cands = [_entry("x", "knowledge/x.md", "y", "z")]
    assert _keyword_fallback_select("完全无关的查询词", cands) == []
