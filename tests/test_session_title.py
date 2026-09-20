"""标题单一数据源机制测试。

标题链路：session/title 事件 → 事件日志持久化 → 投影缓存（projcache）→ list_sessions/详情 API。
覆盖：属性派生、进程重启重建、checkpoint 即时落盘、last-wins、不进 LLM 上下文、列表命名。
"""
from __future__ import annotations

import json

import pytest


@pytest.fixture
def session_env(tmp_path, monkeypatch):
    """隔离 session 目录 + 重置全局单例（投影缓存/后端按目录构造，须随 env 重建）。"""
    monkeypatch.setenv("MYCODE_SESSION_DIR", str(tmp_path))
    import agents.core.session as sess
    import agents.core.session_projection_cache as spc
    sess._backend = None
    spc._projection_cache = None
    yield tmp_path
    sess._backend = None
    spc._projection_cache = None


class TestSessionTitle:
    def test_title_property_from_projection(self, session_env):
        from agents.core.session import Session
        s = Session(session_id="t1")
        assert s.title is None
        s.append("session/title", {"title": "My Title"})
        assert s.title == "My Title"
        assert s.projections["title"] == "My Title"

    def test_title_persisted_and_rebuilt_after_restart(self, session_env):
        from agents.core.session import Session
        s = Session(session_id="t2")
        s.append("user_message", {"content": "hi"})
        s.append("session/title", {"title": "Persisted"})
        # 模拟进程重启：全新实例从事件日志重建
        loaded = Session.load_from_events("t2")
        assert loaded is not None
        assert loaded.title == "Persisted"

    def test_title_event_forces_checkpoint_write(self, session_env):
        from agents.core.session import Session
        s = Session(session_id="t3")
        s.append("session/title", {"title": "Checkpointed"})
        projcache = session_env / "t3.projcache.json"
        assert projcache.exists()
        data = json.loads(projcache.read_text())
        assert data["rows"]["title"]["val"] == "Checkpointed"
        assert data["format_version"] == 1

    def test_last_title_wins(self, session_env):
        from agents.core.session import Session
        s = Session(session_id="t4")
        s.append("session/title", {"title": "First"})
        s.append("session/title", {"title": "Second"})
        loaded = Session.load_from_events("t4")
        assert loaded.title == "Second"

    def test_title_event_not_in_llm_messages(self, session_env):
        from agents.core.session import Session
        s = Session(session_id="t5")
        s.append("user_message", {"content": "hello"})
        s.append("session/title", {"title": "T"})
        msgs = s.get_messages_for_llm()
        assert len(msgs) == 1
        assert msgs[0]["role"] == "user"

    def test_list_sessions_name_from_projcache(self, session_env):
        from agents.core.session import Session, list_sessions
        s = Session(session_id="t6")
        s.append("session/title", {"title": "Listed"})
        names = {x["id"]: x["name"] for x in list_sessions()}
        assert names["t6"] == "Listed"

    def test_list_sessions_fallback_to_id(self, session_env):
        from agents.core.session import Session, list_sessions
        s = Session(session_id="t7")
        s.append("user_message", {"content": "hello"})
        names = {x["id"]: x["name"] for x in list_sessions()}
        assert names["t7"] == "t7"
