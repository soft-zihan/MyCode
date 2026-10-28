"""取消回执：只有事件流承认停了，才算停了。

对应 REST 端点 POST /api/sessions/{id}/abort。它过去在 agent.abort() 之后立刻写
turn/cancel，而 abort 只是设标志 + cancel task，工具与子进程还没停——事件流于是
记了一个假的「已停止」。这组测试钉住新语义：等到收尾事件出现才写 confirmed=true，
等不到就如实写 false，绝不猜。
"""

from __future__ import annotations

import asyncio
import time

import pytest

from agents.core.cancel_settlement import settled_since, wait_for_turn_settlement


@pytest.fixture
def session_env(tmp_path, monkeypatch):
    monkeypatch.setenv("MYCODE_SESSION_DIR", str(tmp_path / "sessions"))
    import agents.core.session as sess
    import agents.core.session_projection_cache as spc

    monkeypatch.setattr(sess, "_backend", None)
    monkeypatch.setattr(spc, "_projection_cache", None)
    yield tmp_path
    monkeypatch.setattr(sess, "_backend", None)
    monkeypatch.setattr(spc, "_projection_cache", None)


def _running_session(session_env, sid: str = "s-cancel"):
    from agents.core.session import Session

    s = Session(session_id=sid)
    s.append("turn/start", {"turn": 1})
    s.append("user_message", {"content": "跑一下测试"})
    return s


# ── 判定：什么才算「停了」 ─────────────────────────────────────────

def test_open_turn_is_not_settled(session_env):
    s = _running_session(session_env)
    assert settled_since(s, 0) is False


def test_turn_end_settles(session_env):
    s = _running_session(session_env)
    since = s.seq
    s.append("turn/end", {"turn": 1, "reason": "aborted"})
    assert settled_since(s, since) is True


def test_turn_cancel_settles(session_env):
    s = _running_session(session_env)
    since = s.seq
    s.append("turn/cancel", {"reason": "user_abort", "turn": 1})
    assert settled_since(s, since) is True


def test_closer_from_an_earlier_turn_does_not_count(session_env):
    """取消之前那个 turn 的收尾事件不能拿来当本次的证据——否则「一直停不下来」
    会被上一轮的 turn/end 掩盖成「已确认停止」。"""
    s = _running_session(session_env)
    s.append("turn/end", {"turn": 1, "reason": "completed"})
    since = s.seq                      # 取消发生在此刻
    s.append("turn/start", {"turn": 2})
    assert settled_since(s, since) is False


# ── 等待：超时就如实说没确认 ───────────────────────────────────────

@pytest.mark.asyncio
async def test_wait_times_out_and_says_so(session_env):
    s = _running_session(session_env)
    since = s.seq
    t0 = time.monotonic()
    assert await wait_for_turn_settlement(s, since, timeout_s=0.2) is False
    elapsed = time.monotonic() - t0
    assert elapsed >= 0.2, "没真的等就返回了，这条断言会变成空转"
    assert elapsed < 2.0


@pytest.mark.asyncio
async def test_wait_returns_as_soon_as_settlement_lands(session_env):
    s = _running_session(session_env)
    since = s.seq

    async def unwind():
        await asyncio.sleep(0.12)
        s.append("turn/end", {"turn": 1, "reason": "aborted"})

    task = asyncio.create_task(unwind())
    t0 = time.monotonic()
    ok = await wait_for_turn_settlement(s, since, timeout_s=5.0)
    elapsed = time.monotonic() - t0
    await task
    assert ok is True
    assert elapsed < 1.0, f"收尾已经落盘却还等了 {elapsed:.2f}s"


# ── 端点：写进事件流的必须是核实过的事实 ───────────────────────────

class _FakeAgent:
    """只暴露端点读到的那几样：abort()、is_processing、_current_turn。"""

    def __init__(self, session, *, processing: bool, settles: bool):
        self._session = session
        self._processing = processing
        self._settles = settles
        self._current_turn = 1
        self.abort_calls = 0

    @property
    def is_processing(self) -> bool:
        return self._processing

    def abort(self) -> None:
        self.abort_calls += 1
        self._processing = False
        if self._settles:
            # 模拟 turn_runner 的 CancelledError 分支：解绕之后才写收尾事件
            self._session.append("turn/end", {"turn": self._current_turn, "reason": "aborted"})


class _FakeManager:
    def __init__(self, agent, session):
        self._agent = agent
        self._session = session

    def get_agent(self, session_id):
        return self._agent

    def get(self, session_id):
        return self._session


def _patch_manager(monkeypatch, agent, session):
    import agents.session_manager as sm

    monkeypatch.setattr(sm, "get_session_manager", lambda: _FakeManager(agent, session))


@pytest.mark.asyncio
async def test_endpoint_confirms_after_real_settlement(session_env, monkeypatch):
    from frontend.server.routers.sessions import api_abort_session

    s = _running_session(session_env)
    agent = _FakeAgent(s, processing=True, settles=True)
    _patch_manager(monkeypatch, agent, s)

    result = await api_abort_session("s-cancel")

    assert result["success"] is True
    assert result["confirmed"] is True
    cancel = [e for e in s.events if e["type"] == "turn/cancel"]
    assert len(cancel) == 1 and cancel[0]["confirmed"] is True
    # 顺序是关键：收尾事件必须在取消记录之前，否则又变成「先宣布停了，后才停」
    types = [e["type"] for e in s.events]
    assert types.index("turn/end") < types.index("turn/cancel")


@pytest.mark.asyncio
async def test_endpoint_reports_unconfirmed_when_nothing_settles(session_env, monkeypatch):
    import agents.core.cancel_settlement as cs
    from frontend.server.routers.sessions import api_abort_session

    monkeypatch.setattr(cs, "CANCEL_SETTLE_TIMEOUT_S", 0.2)
    s = _running_session(session_env)
    agent = _FakeAgent(s, processing=True, settles=False)
    _patch_manager(monkeypatch, agent, s)

    result = await api_abort_session("s-cancel")

    assert result["confirmed"] is False, "停不下来却说停了，正是要修的那个谎"
    cancel = [e for e in s.events if e["type"] == "turn/cancel"]
    assert len(cancel) == 1 and cancel[0]["confirmed"] is False


@pytest.mark.asyncio
async def test_idle_session_confirms_without_waiting(session_env, monkeypatch):
    """没有东西在跑时，「已停止」是平凡为真的，不该白等一个超时。"""
    import agents.core.cancel_settlement as cs
    from frontend.server.routers.sessions import api_abort_session

    monkeypatch.setattr(cs, "CANCEL_SETTLE_TIMEOUT_S", 30.0)
    s = _running_session(session_env)
    agent = _FakeAgent(s, processing=False, settles=False)
    _patch_manager(monkeypatch, agent, s)

    t0 = time.monotonic()
    result = await api_abort_session("s-cancel")
    assert result["confirmed"] is True
    assert time.monotonic() - t0 < 1.0
