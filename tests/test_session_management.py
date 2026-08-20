"""/sessions 管理：目录隔离、删除、清理、表格渲染辅助函数。"""

from __future__ import annotations

import json

from agents.session import (
    session_dir,
    save_session,
    list_sessions,
    delete_session,
    clean_sessions,
)
from agents.ui import _shorten_path, _relative_time


def _mk_session(sid: str, cwd: str, start: str) -> None:
    save_session(sid, {"metadata": {"id": sid, "cwd": cwd, "startTime": start, "messageCount": 2}})


# ─── 目录隔离 ───────────────────────────────────────────────

def test_session_dir_respects_env(tmp_path, monkeypatch):
    monkeypatch.setenv("BEAR_SESSION_DIR", str(tmp_path / "s"))
    assert session_dir() == tmp_path / "s"


def test_session_dir_default_under_home(monkeypatch):
    monkeypatch.delenv("BEAR_SESSION_DIR", raising=False)
    from pathlib import Path
    assert session_dir() == Path.home() / ".bear-code" / "sessions"


# ─── 删除 ───────────────────────────────────────────────────

def test_delete_session_removes_file(tmp_path, monkeypatch):
    monkeypatch.setenv("BEAR_SESSION_DIR", str(tmp_path))
    _mk_session("abc123", "/tmp/x", "2026-08-20T10:00:00Z")
    assert len(list_sessions()) == 1
    assert delete_session("abc123") is True
    assert len(list_sessions()) == 0


def test_delete_session_missing_returns_false(tmp_path, monkeypatch):
    monkeypatch.setenv("BEAR_SESSION_DIR", str(tmp_path))
    assert delete_session("nonexistent") is False


# ─── 清理测试残留 ───────────────────────────────────────────

def test_clean_sessions_only_tmp(tmp_path, monkeypatch):
    monkeypatch.setenv("BEAR_SESSION_DIR", str(tmp_path))
    _mk_session("real1", "/Users/han/proj", "2026-08-20T10:00:00Z")
    _mk_session("tmp1", "/private/var/folders/x/pytest-of-han/pytest-1/t/workdir", "2026-08-20T09:00:00Z")
    _mk_session("tmp2", "/tmp/whatever", "2026-08-20T08:00:00Z")
    deleted = clean_sessions(only_tmp=True)
    assert deleted == 2
    remaining = [s["id"] for s in list_sessions()]
    assert remaining == ["real1"]


def test_clean_sessions_keep_latest(tmp_path, monkeypatch):
    monkeypatch.setenv("BEAR_SESSION_DIR", str(tmp_path))
    for i in range(5):
        _mk_session(f"s{i}", "/Users/han/p", f"2026-08-20T0{i}:00:00Z")
    deleted = clean_sessions(keep_latest=2)
    assert deleted == 3
    remaining = sorted(s["id"] for s in list_sessions())
    assert remaining == ["s3", "s4"]


# ─── 路径缩写 ───────────────────────────────────────────────

def test_shorten_path_home_prefix(monkeypatch):
    from pathlib import Path
    home = str(Path.home())
    assert _shorten_path(home) == "~"
    assert _shorten_path(home + "/x") == "~/x"


def test_shorten_path_keeps_project_name():
    long_path = "/Users/han/Documents/projects/Learn-LLM-and-Agent/BearCode"
    result = _shorten_path(long_path, max_len=44)
    assert result.endswith("BearCode")
    assert result.startswith("…/")
    assert len(result) <= 44


def test_shorten_path_short_unchanged():
    assert _shorten_path("/short/path") == "/short/path"


# ─── 相对时间 ───────────────────────────────────────────────

def test_relative_time_invalid_returns_input():
    assert _relative_time("not-a-time") == "not-a-time"
    assert _relative_time("") == "?"


def test_relative_time_format():
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert _relative_time(now) == "just now"
