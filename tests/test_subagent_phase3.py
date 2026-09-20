"""子智能体 Session 生命周期测试。

当前架构：
- 子 Agent 拥有独立的内存 Session，用于 WebSocket 广播和消息派生。
- 子 Session 不写入主会话事件后端；父会话通过 sub_agent/start/end 事件记录生命周期。
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


def test_sub_agent_session_is_memory_only(session_env):
    from agents.core.session import Session

    sub_session = Session(
        session_id="child_1",
        parent_session="parent_1",
        origin="sub_agent",
        agent_type="explore",
    )
    sub_session.append("user_message", {"content": "task"})
    sub_session.append("assistant_message", {"content": "result"})

    assert len(sub_session._log) == 2
    assert not (session_env / "child_1.events.jsonl").exists()
    assert Session.load_from_events("child_1") is None


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
