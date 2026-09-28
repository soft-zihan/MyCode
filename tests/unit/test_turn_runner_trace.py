"""BC-22 回归：成功路径 turn/end 事件 trace_id 从 turn_span 取，不受字段清空时序影响。"""
import time
from types import SimpleNamespace

from agents.core.turn_runner import TurnRunner


class _FakeSpan:
    def __init__(self, trace_id):
        self._trace_id = trace_id
        self.updated = None

    def get_trace_id(self):
        return self._trace_id

    def update(self, **kwargs):
        self.updated = kwargs


def _stub_agent():
    events = []
    session = SimpleNamespace(
        append=lambda type_, payload: events.append((type_, payload)),
        seq=7,
    )
    return SimpleNamespace(
        session=session,
        _current_turn=3,
        _current_sub_agent_id=None,
        _current_trace_id=None,          # finally 已清空——旧实现在此读到 None
        _loop_guard_stop_reason=None,
        _tool_budget_stop_reason=None,
        _aborted=False,
        _tool_call_count=2,
        _failed_tool_call_count=0,
        max_tool_calls=50,
        total_input_tokens=100,
        total_output_tokens=40,
    ), events


def test_finalize_turn_end_carries_span_trace_id():
    agent, events = _stub_agent()
    runner = TurnRunner(agent)
    span = _FakeSpan("trace-abc123")
    runner._finalize_turn_events(span, "done", time.time(), 90, 30)
    turn_end = [p for t, p in events if t == "turn/end"]
    assert len(turn_end) == 1
    assert turn_end[0]["trace_id"] == "trace-abc123"
    assert turn_end[0]["reason"] == "completed"
    assert turn_end[0]["turn"] == 3


def test_finalize_loop_guard_reason_still_carries_trace_id():
    agent, events = _stub_agent()
    agent._loop_guard_stop_reason = "repeat"
    runner = TurnRunner(agent)
    span = _FakeSpan("trace-lg")
    runner._finalize_turn_events(span, "", time.time(), 0, 0)
    turn_end = [p for t, p in events if t == "turn/end"][0]
    assert turn_end["reason"] == "loop_guard"
    assert turn_end["loop_guard_reason"] == "repeat"
    assert turn_end["trace_id"] == "trace-lg"
