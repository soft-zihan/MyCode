"""U6 崩溃恢复：启动扫描分类（正常/悬挂/脏缓存/torn tail）+ 优雅 shutdown + turn/cancel 一致性。"""

from __future__ import annotations

import asyncio
import json

import pytest

from agents.core.session import Session, get_session_backend
from agents.core.session_crash_recovery import (
    detect_torn_tail,
    scan_and_mark_interrupted,
    shutdown_active_sessions,
    unpaired_turn_depth,
)


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


def _force_projcache(session: Session) -> None:
    from agents.core.session_projection_cache import (
        ProjectionCheckpoint,
        get_projection_cache,
        get_projection_registry,
    )
    reg = get_projection_registry()
    rows = reg.checkpoint(session._projections, max(session.seq - 1, 0))
    get_projection_cache().save_checkpoint(ProjectionCheckpoint.from_session(session, rows))


def _projcache_running(session_id: str) -> bool | None:
    from agents.core.session import session_dir
    path = session_dir() / f"{session_id}.projcache.json"
    if not path.exists():
        return None
    rows = json.loads(path.read_text()).get("rows", {})
    return (rows.get("running") or {}).get("val")


def _make_hung_session(session_id: str) -> Session:
    s = Session(session_id=session_id)
    s.append("turn/start", {"turn": 1})
    s.append("user_message", {"content": "hi"})
    _force_projcache(s)
    return s


def test_scan_marks_hung_session(session_env):
    _make_hung_session("hung-1")
    assert _projcache_running("hung-1") is True

    repaired = scan_and_mark_interrupted()
    assert repaired == [{"session_id": "hung-1", "unpaired_turns": 1}]

    events = get_session_backend().load_all_events("hung-1")
    # closer 由 load_from_events 合成并落盘（恢复是数据库的属性）
    ends = [e for e in events if e["type"] == "turn/end" and e.get("reason") == "crash_recovery"]
    assert len(ends) == 1 and ends[0].get("synthesized") is True
    notes = [e for e in events if e["type"] == "session/interrupted"]
    assert len(notes) == 1 and notes[0]["reason"] == "startup_scan"
    # seq 连续无空洞（内存修复占用 seq 曾导致永久空洞——U6 落盘修复的回归锚点）
    seqs = [e["seq"] for e in events]
    assert seqs == list(range(len(seqs)))
    # 投影恢复：running=False（前端不再永久"运行中"）
    assert _projcache_running("hung-1") is False
    # 幂等：再扫不再处理
    assert scan_and_mark_interrupted() == []


def test_scan_skips_normal_session(session_env):
    s = Session(session_id="normal-1")
    s.append("turn/start", {"turn": 1})
    s.append("user_message", {"content": "hi"})
    s.append("assistant_message", {"content": "hello"})
    s.append("turn/end", {"turn": 1})
    _force_projcache(s)
    n_before = len(get_session_backend().load_all_events("normal-1"))

    assert scan_and_mark_interrupted() == []
    assert len(get_session_backend().load_all_events("normal-1")) == n_before
    assert _projcache_running("normal-1") is False


def test_scan_refreshes_stale_cache_without_writing_events(session_env):
    """脏缓存：事件流已平衡但 projcache 卡在 running=True → 只重建 checkpoint。"""
    from agents.core.session_projection_cache import (
        FORMAT_VERSION,
        ProjectionCheckpoint,
        get_projection_cache,
        get_projection_registry,
    )

    s = Session(session_id="stale-1")
    s.append("turn/start", {"turn": 1})
    s.append("turn/end", {"turn": 1})
    n_events = len(get_session_backend().load_all_events("stale-1"))

    reg = get_projection_registry()
    state = reg.init_state()
    state["running"] = True
    cp = ProjectionCheckpoint(
        session_id="stale-1", seq=0, rows=reg.checkpoint(state, 0),
        format_version=FORMAT_VERSION, created_at=0, cwd=None,
    )
    get_projection_cache().save_checkpoint(cp)
    assert _projcache_running("stale-1") is True

    assert scan_and_mark_interrupted() == []
    assert len(get_session_backend().load_all_events("stale-1")) == n_events
    assert _projcache_running("stale-1") is False


def test_scan_repairs_torn_tail_combo(session_env):
    """torn tail（seq 缺口）+ 悬挂 turn 组合：核验以事件流为准，照常收口。"""
    backend = get_session_backend()
    backend.append("torn-1", {"type": "turn/start", "seq": 0, "time": 1, "session_id": "torn-1"})
    backend.append("torn-1", {"type": "user_message", "seq": 2, "time": 2, "session_id": "torn-1", "content": "x"})

    from agents.core.session_projection_cache import (
        FORMAT_VERSION,
        ProjectionCheckpoint,
        get_projection_cache,
        get_projection_registry,
    )
    reg = get_projection_registry()
    state = reg.init_state()
    state["running"] = True
    get_projection_cache().save_checkpoint(ProjectionCheckpoint(
        session_id="torn-1", seq=2, rows=reg.checkpoint(state, 2),
        format_version=FORMAT_VERSION, created_at=0, cwd=None,
    ))

    repaired = scan_and_mark_interrupted()
    assert repaired == [{"session_id": "torn-1", "unpaired_turns": 1}]
    events = backend.load_all_events("torn-1")
    assert any(e["type"] == "turn/end" and e.get("reason") == "crash_recovery" for e in events)
    assert _projcache_running("torn-1") is False


def test_turn_cancel_counts_as_closer():
    """D4 一致性：turn/cancel 终结 turn——detect/depth 都不再把它当悬挂。"""
    events = [
        {"type": "turn/start", "seq": 0},
        {"type": "turn/cancel", "seq": 1, "reason": "user_abort"},
    ]
    assert detect_torn_tail(events).interrupted_turn is False
    assert unpaired_turn_depth(events) == 0
    hung = [{"type": "turn/start", "seq": 0}]
    assert detect_torn_tail(hung).interrupted_turn is True
    assert unpaired_turn_depth(hung) == 1


class _FakeAgent:
    def __init__(self, session: Session, stubborn: bool = False):
        self.session = session
        self.is_processing = True
        self.abort_called = False
        self._stubborn = stubborn

    def abort(self) -> None:
        self.abort_called = True
        if not self._stubborn:
            self.is_processing = False
            self.session.append("turn/end", {"reason": "aborted"})


class _FakeManager:
    def __init__(self, pairs: list[tuple[Session, _FakeAgent]]):
        self._pairs = pairs

    def active_sessions(self):
        return [s for s, _ in self._pairs]

    def get_agent(self, session_id):
        for s, a in self._pairs:
            if s.id == session_id:
                return a
        return None


@pytest.mark.asyncio
async def test_shutdown_active_sessions(session_env, monkeypatch):
    s1 = Session(session_id="shut-1")
    s1.append("turn/start", {"turn": 1})
    a1 = _FakeAgent(s1)  # abort 后自己收尾
    s2 = Session(session_id="shut-2")
    s2.append("turn/start", {"turn": 1})
    a2 = _FakeAgent(s2, stubborn=True)  # 收不了尾，需要合成

    import agents.session_manager as smgr
    monkeypatch.setattr(smgr, "get_session_manager", lambda: _FakeManager([(s1, a1), (s2, a2)]))

    closed = await shutdown_active_sessions(timeout_s=0.3)

    assert a1.abort_called and a2.abort_called
    assert closed == ["shut-2"]
    # s1：agent 自写 turn/end{aborted}，不再合成
    assert not any(e.get("reason") == "shutdown" for e in s1.events)
    # s2：合成 turn/end{shutdown} + session/interrupted 落盘
    events2 = get_session_backend().load_all_events("shut-2")
    assert any(e["type"] == "turn/end" and e.get("reason") == "shutdown" for e in events2)
    assert any(e["type"] == "session/interrupted" and e.get("reason") == "shutdown" for e in events2)


@pytest.mark.asyncio
async def test_shutdown_no_active_sessions(session_env, monkeypatch):
    import agents.session_manager as smgr
    monkeypatch.setattr(smgr, "get_session_manager", lambda: _FakeManager([]))
    assert await shutdown_active_sessions() == []


def test_scan_cleans_orphan_projcache(session_env):
    """孤儿 projcache（running=True 但零事件）：直接清理，不写任何事件。"""
    from agents.core.session_projection_cache import (
        FORMAT_VERSION,
        ProjectionCheckpoint,
        get_projection_cache,
        get_projection_registry,
    )

    reg = get_projection_registry()
    state = reg.init_state()
    state["running"] = True
    get_projection_cache().save_checkpoint(ProjectionCheckpoint(
        session_id="orphan-1", seq=0, rows=reg.checkpoint(state, 0),
        format_version=FORMAT_VERSION, created_at=0, cwd=None,
    ))

    assert scan_and_mark_interrupted() == []
    assert _projcache_running("orphan-1") is None  # 缓存文件已删


def test_load_session_with_seq_hole_tolerant(session_env):
    """持久流 seq 空洞（历史 crash 残留）：加载填平占位，LLM 消息构建不再 IndexError。"""
    backend = get_session_backend()
    backend.append("hole-1", {"type": "user_message", "seq": 0, "time": 1, "session_id": "hole-1", "content": "a"})
    backend.append("hole-1", {"type": "assistant_message", "seq": 2, "time": 2, "session_id": "hole-1", "content": "b"})

    s = Session.load_from_events("hole-1")
    assert s is not None
    assert s.seq == 3
    msgs = s.get_messages_for_llm()
    assert any(m.get("content") == "a" for m in msgs)
    assert any(m.get("content") == "b" for m in msgs)
    # 补洞后 append 维持 seq 连续（不变量：_log 以 seq 为下标）
    ev = s.append("user_message", {"content": "c"})
    assert ev["seq"] == 3
    assert s.event_at(3) is ev
