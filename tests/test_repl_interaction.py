"""REPL 交互：@ 路径补全、readline 绑定。"""

from __future__ import annotations

import pytest

from agents.main import _complete_at_path, _setup_readline, REPL_COMMANDS


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


def test_setup_readline_uses_libedit_bind_on_macos(monkeypatch):
    import agents.main as m

    calls = []

    class FakeReadline:
        __doc__ = "Importing this module enables command line editing using libedit readline."

        def set_completer_delims(self, d):
            calls.append(("delims", d))

        def set_completer(self, fn):
            calls.append(("completer", fn))

        def parse_and_bind(self, s):
            calls.append(("bind", s))

    monkeypatch.setitem(__import__("sys").modules, "readline", FakeReadline())
    m._setup_readline()
    binds = [v for k, v in calls if k == "bind"]
    assert binds == ['bind "^I" rl_complete']


def test_setup_readline_uses_gnu_bind_on_gnu(monkeypatch):
    import agents.main as m

    calls = []

    class FakeReadline:
        __doc__ = "Importing this module enables command line editing using GNU readline."

        def set_completer_delims(self, d):
            calls.append(("delims", d))

        def set_completer(self, fn):
            calls.append(("completer", fn))

        def parse_and_bind(self, s):
            calls.append(("bind", s))

    monkeypatch.setitem(__import__("sys").modules, "readline", FakeReadline())
    m._setup_readline()
    binds = [v for k, v in calls if k == "bind"]
    assert binds == ["tab: complete"]
