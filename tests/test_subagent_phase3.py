"""子智能体 Session 生命周期测试。

U2 架构：
- 子 Agent 拥有独立 Session，事件与主会话一样落盘（可观测/可恢复/可续跑）。
- 子会话首事件 session/meta 携带归属（origin/parent_session/agent_type），
  是列表过滤与续跑归属校验的唯一数据源；父会话通过 sub_agent/start/end 记录生命周期。
"""

from __future__ import annotations

import pytest


@pytest.fixture
def session_env(tmp_path, monkeypatch):
    monkeypatch.setenv("MYCODE_SESSION_DIR", str(tmp_path))
    import agents.core.session as sess
    import agents.core.session_projection_cache as spc

    monkeypatch.setattr(sess, "_backend", None)
    monkeypatch.setattr(spc, "_projection_cache", None)
    yield tmp_path
    monkeypatch.setattr(sess, "_backend", None)
    monkeypatch.setattr(spc, "_projection_cache", None)


def test_session_init_with_parent(session_env):
    from agents.core.session import Session

    session = Session(
        session_id="child_1",
        parent_session="parent_1",
        origin="sub_agent",
        agent_type="explore",
    )

    assert session.id == "child_1"
    assert session.parent_session == "parent_1"
    assert session.origin == "sub_agent"
    assert session.agent_type == "explore"


def test_sub_agent_session_is_persisted_with_meta(session_env):
    """U2：子会话事件落盘（可观测/可续跑），session/meta 保存归属且重载可恢复。"""
    from agents.core.session import Session, get_session_backend, is_derived_session_meta

    sub_session = Session(
        session_id="child_1",
        parent_session="parent_1",
        origin="sub_agent",
        agent_type="explore",
    )
    sub_session.append("session/meta", {
        "origin": "sub_agent",
        "parent_session": "parent_1",
        "agent_type": "explore",
    })
    sub_session.append("user_message", {"content": "task"})
    sub_session.append("assistant_message", {"content": "result"})

    assert len(sub_session._log) == 3
    assert get_session_backend().session_exists("child_1")

    loaded = Session.load_from_events("child_1")
    assert loaded is not None
    assert len(loaded.events) == 3
    assert loaded.origin == "sub_agent"
    assert loaded.parent_session == "parent_1"
    assert loaded.agent_type == "explore"

    # 派生会话（sub_agent/eval）不进用户会话列表/清理/最近会话
    assert is_derived_session_meta({"origin": "sub_agent"})
    assert is_derived_session_meta({"origin": "eval"})
    assert not is_derived_session_meta({"origin": None})
    assert not is_derived_session_meta({})


def test_parent_session_records_sub_agent_lifecycle(session_env):
    from agents.core.session import Session

    parent = Session(session_id="parent_1")
    parent.append("user_message", {"content": "use sub-agent"})
    parent.append("sub_agent/start", {
        "agent_id": "sub_1",
        "agent_type": "explore",
        "description": "test task",
        "sub_session_id": "sub_1",
    })

    sub_session = Session(
        session_id="sub_1",
        parent_session="parent_1",
        origin="sub_agent",
        agent_type="explore",
    )
    sub_session.append("user_message", {"content": "test task"})
    sub_session.append("assistant_message", {"content": "result"})

    parent.append("sub_agent/end", {
        "agent_id": "sub_1",
        "status": "completed",
        "summary": "result",
        "sub_session_id": "sub_1",
    })

    assert len(parent._log) == 3
    assert [e["type"] for e in parent._log] == ["user_message", "sub_agent/start", "sub_agent/end"]
    assert parent._log[1]["sub_session_id"] == "sub_1"
    assert parent._log[2]["status"] == "completed"

    reloaded = Session.load_from_events("parent_1")
    assert reloaded is not None
    assert len(reloaded._log) == 3
    assert any(
        e.get("type") == "sub_agent/start" and e.get("sub_session_id") == "sub_1"
        for e in reloaded._log
    )
