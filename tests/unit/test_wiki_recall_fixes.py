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


# ── BC-3：提取管道加固 ──

from agents.wiki.wiki_compiler import _parse_json_arrays, _coerce_extraction_fields


def test_parse_json_arrays_plain():
    found, items = _parse_json_arrays('[{"type": "knowledge", "name": "a"}]')
    assert found and items == [{"type": "knowledge", "name": "a"}]


def test_parse_json_arrays_in_prose():
    text = '好的，提取结果如下：\n[{"name": "x"}]\n以上就是全部。'
    found, items = _parse_json_arrays(text)
    assert found and items == [{"name": "x"}]


def test_parse_json_arrays_concatenated():
    # BC-3 失败形态1：多个 JSON 文档拼接（json.loads 报 Extra data）
    text = '[{"name": "a"}]\n[{"name": "b"}]'
    found, items = _parse_json_arrays(text)
    assert found and [i["name"] for i in items] == ["a", "b"]


def test_parse_json_arrays_empty_is_success():
    found, items = _parse_json_arrays("[]")
    assert found and items == []


def test_parse_json_arrays_none_found():
    found, items = _parse_json_arrays("这段对话没有值得提取的知识。")
    assert not found and items == []


def test_parse_json_arrays_brackets_in_strings():
    text = '[{"content": "数组 [1,2] 和 \\" 引号"}]'
    found, items = _parse_json_arrays(text)
    assert found and items[0]["content"] == '数组 [1,2] 和 " 引号'


def test_coerce_extraction_fields():
    # BC-3 失败形态2：content 是 dict/list → 强转 JSON 文本而非 TypeError
    item = {"name": "x", "content": {"k": "v"}, "description": ["a", "b"]}
    _coerce_extraction_fields(item)
    assert item["content"] == '{"k": "v"}'
    assert item["description"] == '["a", "b"]'


def test_register_compile_failure_poison_pill(ws):
    from agents.core.frontmatter import parse_frontmatter
    from agents.wiki.wiki_capture import (
        capture_session_to_session, register_compile_failure,
        list_uncompiled_segments, get_last_extract_pos, MAX_COMPILE_ATTEMPTS,
    )

    events = [{"seq": i, "type": "user_message", "content": f"m{i}"} for i in range(1, 6)]
    seg = capture_session_to_session("sess-bc3", events, 1)
    assert seg is not None

    assert register_compile_failure(seg) is False   # 第1次
    assert register_compile_failure(seg) is False   # 第2次
    assert seg in list_uncompiled_segments()
    assert register_compile_failure(seg) is True    # 第3次 → 终态
    meta = parse_frontmatter(seg.read_text()).meta
    assert meta["compiled"] == "failed"
    assert meta["compile_attempts"] == str(MAX_COMPILE_ATTEMPTS)
    # 终态后不再出现在待重试列表，水位线推进到 max_seq
    assert seg not in list_uncompiled_segments()
    assert get_last_extract_pos("sess-bc3") == 5
