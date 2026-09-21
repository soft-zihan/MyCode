"""REPL 增强：批量索引解析、@ 引用展开、状态行、窗口/阈值配置。"""

from __future__ import annotations

import pytest

from agents.core.context_edit import parse_index_spec
from agents.main import _expand_at_references
from agents.agent import Agent


# ─── parse_index_spec ───────────────────────────────────────

def test_parse_single_and_list():
    assert parse_index_spec("1") == [1]
    assert parse_index_spec("1 3 5") == [1, 3, 5]
    assert parse_index_spec("1,3,5") == [1, 3, 5]


def test_parse_range_tilde():
    assert parse_index_spec("5~10") == [5, 6, 7, 8, 9, 10]


def test_parse_range_dash():
    assert parse_index_spec("5-10") == [5, 6, 7, 8, 9, 10]


def test_parse_mixed_comma_space_and_range():
    assert parse_index_spec("1,3,5~10") == [1, 3, 5, 6, 7, 8, 9, 10]
    assert parse_index_spec("1 3, 5~7 9") == [1, 3, 5, 6, 7, 9]


def test_parse_dedup_and_sort():
    assert parse_index_spec("3,1,3,2~4") == [1, 2, 3, 4]


def test_parse_reversed_range_normalized():
    assert parse_index_spec("10~5") == [5, 6, 7, 8, 9, 10]


def test_parse_empty_returns_empty():
    assert parse_index_spec("") == []
    assert parse_index_spec("  , ") == []


def test_parse_invalid_raises():
    with pytest.raises(ValueError):
        parse_index_spec("abc")
    with pytest.raises(ValueError):
        parse_index_spec("1~x")


# ─── @ 引用展开 ─────────────────────────────────────────────

def test_at_ref_injects_file_content(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    f = tmp_path / "note.txt"
    f.write_text("hello world", encoding="utf-8")
    text, notes = _expand_at_references("总结一下 @note.txt")
    assert "hello world" in text
    assert "<file path=" in text
    assert notes and "note.txt" in notes[0]


def test_at_ref_directory_listing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "sub").mkdir()
    (tmp_path / "a.py").write_text("x", encoding="utf-8")
    text, notes = _expand_at_references("看看 @.")
    assert "<directory path=" in text
    assert "a.py" in text
    assert "sub/" in text


def test_at_ref_missing_path_left_as_is(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    text, notes = _expand_at_references("联系 foo@missing.example 帮忙")
    assert text == "联系 foo@missing.example 帮忙"
    assert notes == []


def test_at_ref_oversized_file_not_injected(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    big = tmp_path / "big.bin"
    big.write_bytes(b"x" * (40 * 1024))
    text, notes = _expand_at_references("读 @big.bin")
    assert "big.bin" not in text.replace("@big.bin", "")  # 未注入内容块
    assert any("too large" in n for n in notes)


def test_at_ref_no_ref_unchanged():
    text, notes = _expand_at_references("普通消息，没有引用")
    assert text == "普通消息，没有引用"
    assert notes == []


# ─── 状态行与窗口/阈值配置 ──────────────────────────────────

def _make_agent(**kwargs):
    defaults = dict(model="deepseek-chat", api_base="https://example.com/v1", api_key="sk-test")
    defaults.update(kwargs)
    return Agent(**defaults)


def test_status_line_contains_model_and_tokens():
    agent = _make_agent()
    agent.total_input_tokens = 1234
    agent.total_output_tokens = 567
    agent.set_last_usage_tokens(800, 890)
    line = agent.status_line()
    assert "deepseek-chat" in line
    assert "890" in line
    assert "session: 1234 in / 567 out" in line
