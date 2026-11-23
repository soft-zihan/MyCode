"""空订阅语义：未订阅的连接只收全局事件，不收会话事件。

改前是「空 = 收全部」，单人多任务并发时打开任务 A 的页面会收到任务 B 的事件。
"""
from __future__ import annotations

import asyncio

import pytest

from frontend.server.routers import websocket as ws


class _FakeWS:
    def __init__(self):
        self.sent: list[dict] = []

    async def send_json(self, data):
        self.sent.append(data)


@pytest.fixture(autouse=True)
def _clear():
    ws._subscribers.clear()
    yield
    ws._subscribers.clear()


def _sub(sessions: set[str]) -> ws.Subscriber:
    s = ws.Subscriber(_FakeWS())
    s.subscribed_sessions = set(sessions)
    ws._subscribers.append(s)
    return s


@pytest.mark.asyncio
async def test_empty_subscription_gets_no_session_events():
    s = _sub(set())
    ws.broadcast_event({"type": "turn/start", "session_id": "aaa"}, target_session_id="aaa")
    await asyncio.sleep(0)
    assert s.websocket.sent == []


@pytest.mark.asyncio
async def test_empty_subscription_still_gets_global_events():
    s = _sub(set())
    ws.broadcast_event({"type": "session/created", "session_id": "aaa"})
    await asyncio.sleep(0)
    assert len(s.websocket.sent) == 1


@pytest.mark.asyncio
async def test_subscribed_gets_only_its_session():
    a = _sub({"aaa"})
    ws.broadcast_event({"type": "turn/start", "session_id": "bbb"}, target_session_id="bbb")
    ws.broadcast_event({"type": "turn/start", "session_id": "aaa"}, target_session_id="aaa")
    await asyncio.sleep(0)
    assert [e["session_id"] for e in a.websocket.sent] == ["aaa"]