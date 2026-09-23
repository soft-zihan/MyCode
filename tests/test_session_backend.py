"""SessionBackend 双后端 parity 测试（U7-A1）。

JSONL 与 SQLite 后端必须行为完全一致：append/读取过滤/truncate/删除/
新增的 list_session_ids 与 get_latest_event，以及 session.py 的
delete_session/list_sessions 必须路由到当前 backend。
"""

from __future__ import annotations

import pytest

from agents.core import session as session_mod
from agents.core.session_backend_jsonl import JsonlSessionBackend
from agents.core.session_backend_sqlite import SqliteSessionBackend


@pytest.fixture(params=["jsonl", "sqlite"])
def backend(request, tmp_path):
    if request.param == "jsonl":
        return JsonlSessionBackend(session_dir=tmp_path / "sessions")
    return SqliteSessionBackend(db_path=tmp_path / "sessions.db")


@pytest.fixture
def routed_backend(backend, tmp_path, monkeypatch):
    """把 session.py 的全局 backend 指向测试实例，并重定向会话目录。"""
    monkeypatch.setenv("MYCODE_SESSION_DIR", str(tmp_path / "sessions"))
    session_mod.set_session_backend(backend)
    try:
        yield backend
    finally:
        session_mod.set_session_backend(None)


def _ev(seq: int, type_: str, **kw) -> dict:
    return {"seq": seq, "type": type_, "time": 1000 + seq, **kw}


def test_append_and_load_all(backend):
    backend.append("s1", _ev(0, "user_message", content="hi"))
    backend.append("s1", _ev(1, "assistant_message", content="hello"))
    events = backend.load_all_events("s1")
    assert [e["seq"] for e in events] == [0, 1]
    assert events[0]["content"] == "hi"
    assert backend.session_exists("s1")
    assert backend.get_event_count("s1") == 2
    assert not backend.session_exists("nope")
    assert backend.get_event_count("nope") == 0


def test_get_events_filters(backend):
    for i in range(10):
        backend.append("s1", _ev(i, "text", content=str(i)))
    assert [e["seq"] for e in backend.get_events("s1", before=5)] == [0, 1, 2, 3, 4]
    assert [e["seq"] for e in backend.get_events("s1", from_seq=3, to_seq=6)] == [3, 4, 5]
    # limit 取最后 N 条（时间序保持）
    assert [e["seq"] for e in backend.get_events("s1", limit=3)] == [7, 8, 9]
    assert backend.get_events("missing") == []


def test_truncate(backend):
    for i in range(5):
        backend.append("s1", _ev(i, "text"))
    backend.truncate("s1", keep_seq=3)
    assert [e["seq"] for e in backend.load_all_events("s1")] == [0, 1, 2]
    # truncate 不存在的会话是 no-op
    backend.truncate("missing", keep_seq=1)


def test_delete_backend_events(backend):
    backend.append("s1", _ev(0, "text"))
    backend.delete_session("s1")
    assert not backend.session_exists("s1")
    backend.delete_session("missing")  # no-op


def test_list_session_ids(backend):
    backend.append("bbb", _ev(0, "text"))
    backend.append("aaa", _ev(0, "text"))
    backend.append("aaa", _ev(1, "text"))
    assert backend.list_session_ids() == ["aaa", "bbb"]


def test_get_latest_event(backend):
    backend.append("s1", _ev(0, "stats", input_tokens=10))
    backend.append("s1", _ev(1, "text"))
    backend.append("s1", _ev(2, "stats", input_tokens=99))
    latest = backend.get_latest_event("s1", "stats")
    assert latest is not None and latest["input_tokens"] == 99
    assert backend.get_latest_event("s1", "no_such_type") is None
    assert backend.get_latest_event("missing", "stats") is None


# ─── session.py 路由（regression：sqlite 下事件曾永久残留）─────

def test_delete_session_routes_to_backend(routed_backend):
    routed_backend.append("s1", _ev(0, "user_message", content="hi"))
    assert session_mod.delete_session("s1") is True
    assert not routed_backend.session_exists("s1")
    assert routed_backend.get_event_count("s1") == 0
    # 不存在任何东西时返回 False
    assert session_mod.delete_session("s1") is False


def test_list_sessions_includes_backend_only_sessions(routed_backend):
    """有事件但 projcache 未落盘的会话也必须能列出（两后端一致）。"""
    routed_backend.append("s2", _ev(0, "user_message", content="x"))
    ids = {s["id"] for s in session_mod.list_sessions()}
    assert "s2" in ids
