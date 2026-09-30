"""U8 事件治理：公开面白名单（event_types 单源）+ 广播过滤 + 重放前向兼容。"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
from pathlib import Path

import pytest

from agents.core.event_types import (
    ALL_KNOWN_EVENT_TYPES,
    INTERNAL_EVENT_TYPES,
    PUBLIC_EVENT_TYPES,
    is_public_event,
)
from agents.core.session import Session, get_session_backend


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


def _load_websocket_module():
    """单文件加载 routers/websocket.py（绕开 routers/__init__ 的全量 router 导入）。"""
    path = Path(__file__).resolve().parents[2] / "frontend" / "server" / "routers" / "websocket.py"
    spec = importlib.util.spec_from_file_location("ws_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# 2026-09-24 后端发射面全量扫描基线——新增事件类型必须同步 event_types.py，
# 否则本测试红（清单外类型在 load 时还会触发未知类型 warning）
EMITTED_TYPES = {
    "assistant_message", "context/compacted", "error", "events_hidden",
    "memory_injection", "permission/mode_changed", "permission/request",
    "permission/resolved", "plan/updated", "question/request", "question/resolved",
    "repeated_tool_calls", "rewind", "session/created", "session/interrupted",
    "session/meta", "session/plan_linked", "session/title", "session_folded",
    "stats", "step/end", "step/start", "steer/delivered", "steer/dropped",
    "steer/queued", "sub_agent/cancel", "sub_agent/end", "sub_agent/resume",
    "sub_agent/start", "subagent/completed", "task_list/updated", "text", "thinking",
    "tool_call", "tool_folded", "tool_result", "tool_result_msg", "turn/cancel",
    "turn/end", "turn/start", "user_message",
    "eval/task_metadata",
}

FRONTEND_CONSUMED_TYPES = {
    # useChatNodes/useChat/EventRouter/EvalPage 实际消费面（2026-09-30 扫描）
    "thinking", "text", "tool_call", "tool_result",
    "user_message", "error", "stats", "turn/start", "turn/end",
    "context/compacted", "tool_folded", "session_folded",
    "session/created", "session/title", "session/plan_linked", "session/interrupted",
    "permission/request", "permission/mode_changed",
    "question/request", "question/resolved",
    "task_list/updated", "plan/updated",
    "sub_agent/start", "sub_agent/end", "subagent/completed",
}


class TestManifest:
    def test_manifest_covers_all_emitted_types(self):
        assert EMITTED_TYPES <= ALL_KNOWN_EVENT_TYPES, EMITTED_TYPES - ALL_KNOWN_EVENT_TYPES

    def test_frontend_consumed_types_are_public(self):
        assert FRONTEND_CONSUMED_TYPES <= PUBLIC_EVENT_TYPES

    def test_public_internal_disjoint(self):
        assert not (PUBLIC_EVENT_TYPES & INTERNAL_EVENT_TYPES)

    def test_internal_and_unknown_not_public(self):
        for t in INTERNAL_EVENT_TYPES:
            assert not is_public_event(t), t
        assert not is_public_event("future/event_v9")

    def test_eval_prefix_public_with_boundary(self):
        assert is_public_event("eval/run_started")
        assert is_public_event("eval/judge_finished")
        assert not is_public_event("evaluation/x")  # 前缀须带 /


class TestBroadcastFilter:
    def test_broadcast_only_public_events(self, monkeypatch):
        ws = _load_websocket_module()
        sent: list[str] = []

        class FakeWebSocket:
            async def send_json(self, payload):
                sent.append(payload["type"])

        subscriber = ws.Subscriber(FakeWebSocket())
        monkeypatch.setattr(ws, "_subscribers", [subscriber])

        async def run():
            ws.broadcast_event({"type": "turn/end", "session_id": "s1"})
            ws.broadcast_event({"type": "steer/queued", "session_id": "s1"})  # 内部审计
            ws.broadcast_event({"type": "future/event_v9", "session_id": "s1"})  # 未知
            ws.broadcast_event({"type": "session/meta", "session_id": "s1"})  # 内部
            ws.broadcast_event({"type": "eval/task_started", "session_id": None})  # 前缀放行
            await asyncio.sleep(0)  # 让 create_task 的发送执行

        asyncio.run(run())
        assert sent == ["turn/end", "eval/task_started"]


class TestReplayForwardCompat:
    def test_load_unknown_type_warns_and_skips(self, session_env, caplog):
        backend = get_session_backend()
        backend.append("u1", {"type": "user_message", "seq": 0, "time": 1, "session_id": "u1", "content": "a"})
        backend.append("u1", {"type": "future/event_v9", "seq": 1, "time": 2, "session_id": "u1", "payload": {"x": 1}})

        with caplog.at_level(logging.WARNING, logger="agents.core.session"):
            s = Session.load_from_events("u1")

        assert s is not None
        assert "future/event_v9" in caplog.text
        # 未知事件不进 LLM 消息，已知事件不受影响
        msgs = s.get_messages_for_llm()
        assert [m for m in msgs if m.get("content") == "a"]
        assert all("payload" not in str(m) for m in msgs)
        # 未知事件不阻塞后续 append
        ev = s.append("user_message", {"content": "b"})
        assert ev["seq"] == 2

    def test_load_known_types_no_warning(self, session_env, caplog):
        backend = get_session_backend()
        backend.append("u2", {"type": "user_message", "seq": 0, "time": 1, "session_id": "u2", "content": "a"})
        backend.append("u2", {"type": "steer/queued", "seq": 1, "time": 2, "session_id": "u2", "content": "b"})

        with caplog.at_level(logging.WARNING, logger="agents.core.session"):
            s = Session.load_from_events("u2")

        assert s is not None
        assert "未知事件类型" not in caplog.text
