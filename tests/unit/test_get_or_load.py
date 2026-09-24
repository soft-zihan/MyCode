"""BC-26：只读端点内存优先——活会话禁止 load_from_events 触发 crash-repair 写。"""

from __future__ import annotations

import pytest

from agents.core.session import Session
from agents.session_manager import SessionManager


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("MYCODE_SESSION_DIR", str(tmp_path))
    import agents.core.session as sess
    import agents.core.session_projection_cache as spc

    monkeypatch.setattr(sess, "_backend", None)
    monkeypatch.setattr(spc, "_projection_cache", None)
    yield tmp_path
    monkeypatch.setattr(sess, "_backend", None)
    monkeypatch.setattr(spc, "_projection_cache", None)


def test_get_or_load_returns_live_instance_without_repair_writes(env):
    """活会话（turn 未闭合）：get_or_load 返回内存实例，不落 crash_recovery closer。"""
    mgr = SessionManager()
    session = Session(session_id="live-sess")
    session.append("turn/start", {"turn": 1, "trigger": "user"})
    session.append("step/start", {"turn": 1, "step": 1})
    mgr._sessions[session.id] = session

    got = mgr.get_or_load(session.id)
    assert got is session, "活会话必须返回内存实例（单写者）"

    from agents.core.session import get_session_backend
    disk = get_session_backend().load_all_events(session.id)
    assert not any(
        e.get("type") == "turn/end" and e.get("reason") == "crash_recovery" for e in disk
    ), "get_or_load 不得对活会话触发 crash-repair 写"


def test_get_or_load_falls_back_to_disk_for_dead_session(env):
    """非活会话：磁盘加载 + 既有修复语义保持（悬挂 turn 合成 closer）。"""
    dead = Session(session_id="dead-sess")
    dead.append("turn/start", {"turn": 1, "trigger": "user"})

    mgr = SessionManager()
    got = mgr.get_or_load("dead-sess")
    assert got is not None
    assert any(
        e.get("type") == "turn/end" and e.get("reason") == "crash_recovery"
        for e in got.events
    ), "死会话加载应保持 U6 修复语义"


def test_get_or_load_missing_session_returns_none(env):
    mgr = SessionManager()
    assert mgr.get_or_load("no-such-session") is None
