"""REPL 交互增强：thinking 开关、Markdown 自动渲染判定、@ 路径补全。"""

from __future__ import annotations

import pytest

from agents import ui
from agents.main import _complete_at_path, REPL_COMMANDS


# ─── thinking 显示开关 ─────────────────────────────────────

def test_thinking_hidden_by_default():
    ui.set_thinking_visible(False)
    assert ui.thinking_visible() is False


def test_thinking_toggle():
    ui.set_thinking_visible(True)
    assert ui.thinking_visible() is True
    ui.set_thinking_visible(False)
    assert ui.thinking_visible() is False


# ─── Markdown 自动渲染判定 ─────────────────────────────────

def test_md_looks_renderable_with_code_fence():
    assert ui.md_looks_renderable("Here is code:\n```python\nprint(1)\n```\nDone.")


def test_md_looks_renderable_with_heading_and_list():
    assert ui.md_looks_renderable("# Title\n\n- item one\n- item two\n\nmore text here")


def test_md_not_renderable_plain_short_text():
    assert not ui.md_looks_renderable("ok")


def test_md_not_renderable_long_plain_text():
    assert not ui.md_looks_renderable("这是一段没有任何 markdown 特征的普通长文本。" * 5)


def test_md_row_counting_simple():
    # 单行不新增行
    rows, col = ui._md_count_rows("hello", 0, 80)
    assert rows == 0
    assert col == 5


def test_md_row_counting_newline():
    rows, col = ui._md_count_rows("a\nb", 0, 80)
    assert rows == 1
    assert col == 1


def test_md_row_counting_wraps_wide():
    # 81 个字符在 80 宽终端会折行一次
    rows, col = ui._md_count_rows("x" * 81, 0, 80)
    assert rows == 1
    assert col == 1


def test_md_row_counting_cjk_double_width():
    # CJK 全角字符按 2 列计：40 个汉字 = 80 列，恰好一行不折行
    rows, col = ui._md_count_rows("中" * 40, 0, 80)
    assert rows == 0
    assert col == 0  # 80 % 80 == 0


# ─── @ 路径补全 ────────────────────────────────────────────

def test_at_path_completion_lists_files(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "note.txt").write_text("x", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    matches = _complete_at_path("@no")
    assert "@note.txt" in matches


def test_at_path_completion_dir_gets_slash(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "subdir").mkdir()
    matches = _complete_at_path("@sub")
    assert "@subdir/" in matches


def test_at_path_completion_empty_prefix(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "a.py").write_text("x", encoding="utf-8")
    matches = _complete_at_path("@")
    assert any(m.startswith("@") for m in matches)


def test_repl_commands_include_new_ones():
    for cmd in ("/help", "/thinking", "/cd", "/context", "/goal", "/rewind"):
        assert cmd in REPL_COMMANDS
