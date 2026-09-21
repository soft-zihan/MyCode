import asyncio
from types import SimpleNamespace

import pytest

from agents.agent import Agent
from agents.agent_loop import AgentLoop
from agents.core.session import Session
from agents.core.subagent import get_sub_agent_max_tool_calls
from agents.observability.tool_tracker import ToolCallTracker, check_tool_warnings
from agents.tools.dispatcher import ToolDispatcher
from agents.tools.result import ToolExecutionResult


class FakeSubAgent:
    def __init__(self, delay: float = 0.05):
        self.session = None
        self.session_id = None
        self._current_sub_agent_id = None
        self._aborted = False
        self._delay = delay
        self._tool_call_count = 0
        self._failed_tool_call_count = 0
        self._turn_number = 0

    async def run_once(self, prompt: str):
        await asyncio.sleep(self._delay)
        return {"text": "should not reach", "tokens": {"input": 0, "output": 0}}


class FakeAgent:
    def __init__(self):
        self.session = Session(session_id="parent-loop-guard", origin="test")
        self.session_id = self.session.id
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.current_sub_agent_id = None
        self.permission_mode = "bypassPermissions"

    def abort_requested(self) -> bool:
        return False

    def _spawn_sub_agent(self, **kwargs):
        return FakeSubAgent(delay=0.2)


class ProgressFakeSubAgent(FakeSubAgent):
    def __init__(self):
        super().__init__(delay=0.01)
        self._tool_call_count = 8
        self._failed_tool_call_count = 1
        self._turn_number = 2

    async def run_once(self, prompt: str):
        await asyncio.sleep(self._delay)
        return {"text": "progress", "tokens": {"input": 10, "output": 5}}


class ProgressFakeAgent(FakeAgent):
    def _spawn_sub_agent(self, **kwargs):
        return ProgressFakeSubAgent()


def test_identical_failures_block_next_call():
    tracker = ToolCallTracker()
    args = {"path": "foo"}

    for _ in range(2):
        result = check_tool_warnings(tracker, "read_file", args, success=False, outcome="error")
        assert not result["force_stop"]

    result = check_tool_warnings(tracker, "read_file", args, success=False, outcome="error")
    assert result["blocked"]
    assert result["force_stop"]

    decision = tracker.precheck("read_file", args)
    assert decision.action == "block"
    assert "Loop guard blocked" in (decision.message or "")


def test_identical_timeout_blocks_after_two_failures():
    tracker = ToolCallTracker()
    args = {"prompt": "same task"}

    check_tool_warnings(tracker, "agent", args, success=False, outcome="timeout")
    result = check_tool_warnings(tracker, "agent", args, success=False, outcome="timeout")
    assert result["blocked"]
    assert tracker.precheck("agent", args).action == "block"


def test_agent_timeout_with_progress_allows_more_before_tool_block():
    tracker = ToolCallTracker()
    progressing = {"tool_call_count": 8, "failed_tool_call_count": 1, "child_turn_count": 2}

    for i in range(3):
        result = check_tool_warnings(
            tracker,
            "agent",
            {"prompt": f"subtask {i}"},
            success=False,
            outcome="timeout",
            metadata=progressing,
        )
        assert not result["blocked"]

    result = check_tool_warnings(
        tracker,
        "agent",
        {"prompt": "subtask 3"},
        success=False,
        outcome="timeout",
        metadata=progressing,
    )
    assert result["blocked"]
    assert tracker.blocked_tools["agent"]["scope"] == "tool_timeout_streak"


def test_success_resets_failure_block():
    tracker = ToolCallTracker()
    args = {"query": "x"}
    check_tool_warnings(tracker, "web_search", args, success=False, outcome="error")
    check_tool_warnings(tracker, "web_search", args, success=False, outcome="error")
    result = check_tool_warnings(tracker, "web_search", args, success=True, outcome="success")
    assert not result["blocked"]
    assert tracker.precheck("web_search", args).action == "allow"


def test_agent_tool_timeout_defaults_to_900(monkeypatch):
    monkeypatch.delenv("MYCODE_AGENT_TOOL_TIMEOUT", raising=False)
    dispatcher = ToolDispatcher(agent_ref=FakeAgent())
    assert dispatcher.get_tool_timeout("agent") == 900

    monkeypatch.setenv("MYCODE_AGENT_TOOL_TIMEOUT", "123")
    assert dispatcher.get_tool_timeout("agent") == 123


@pytest.mark.asyncio
async def test_execute_tool_call_returns_structured_timeout(monkeypatch):
    class SlowDispatcher(ToolDispatcher):
        def get_tool_timeout(self, name: str) -> int:
            return 0

        async def _execute_tool_call_inner(self, name: str, inp: dict) -> str:
            await asyncio.sleep(0.05)
            return "late"

    dispatcher = SlowDispatcher(agent_ref=FakeAgent())
    result = await dispatcher.execute_tool_call("slow_tool", {})
    assert result.status == "error"
    assert result.outcome == "timeout"
    assert result.metadata["timeout_s"] == 0
    assert "timed out" in result.text


@pytest.mark.asyncio
async def test_agent_timeout_writes_sub_agent_end(monkeypatch):
    monkeypatch.setattr("agents.core.subagent.get_sub_agent_config", lambda agent_type: {
        "system_prompt": "test",
        "tools": [],
        "model_ref": "",
    })

    class TimeoutDispatcher(ToolDispatcher):
        def get_tool_timeout(self, name: str) -> int:
            return 0

    agent = FakeAgent()
    dispatcher = TimeoutDispatcher(agent_ref=agent)
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(
            dispatcher._execute_agent_tool({"type": "general", "prompt": "long task"}),
            timeout=0.05,
        )

    end_events = [e for e in agent.session.events if e.get("type") == "sub_agent/end"]
    assert len(end_events) == 1
    assert end_events[0]["status"] == "timeout"
    assert end_events[0]["outcome"] == "timeout"
    assert end_events[0]["timeout_s"] == 0
    assert end_events[0]["sub_session_id"]


@pytest.mark.asyncio
async def test_sub_agent_end_uses_child_tool_counters(monkeypatch):
    monkeypatch.setattr("agents.core.subagent.get_sub_agent_config", lambda agent_type: {
        "system_prompt": "test",
        "tools": [],
        "model_ref": "",
    })

    agent = ProgressFakeAgent()
    dispatcher = ToolDispatcher(agent_ref=agent)
    result = await dispatcher._execute_agent_tool({"type": "general", "prompt": "progress task"})

    assert result.text == "progress"
    assert result.status == "ok"
    assert result.outcome == "success"
    assert result.metadata["tool_call_count"] == 8
    assert result.metadata["failed_tool_call_count"] == 1
    end_events = [e for e in agent.session.events if e.get("type") == "sub_agent/end"]
    assert len(end_events) == 1
    assert end_events[0]["status"] == "completed"
    assert end_events[0]["tool_call_count"] == 8
    assert end_events[0]["failed_tool_call_count"] == 1
    assert end_events[0]["child_turn_count"] == 2


def test_publish_tool_result_event_counts_outcomes():
    class StubAgent:
        def __init__(self):
            self.session = Session(session_id="counter-agent", origin="test")
            self._current_turn = 1
            self._current_step = 1
            self._current_sub_agent_id = None
            self._tool_call_count = 0
            self._failed_tool_call_count = 0

    stub = StubAgent()
    Agent.publish_tool_result_event(stub, "ok", "read_file", "ok", "ok", outcome="success")
    Agent.publish_tool_result_event(stub, "err", "web_search", "Error", "error", outcome="timeout")
    Agent.publish_tool_result_event(stub, "blocked", "agent", "Blocked", "error", outcome="blocked")

    assert stub._tool_call_count == 3
    assert stub._failed_tool_call_count == 2


def test_near_duplicate_web_search_warns_then_blocks():
    tracker = ToolCallTracker()
    base_query = "ams frozen peas standard pdf issue effective current"

    for i in range(8):
        result = check_tool_warnings(
            tracker,
            "web_search",
            {"query": f"{base_query} variant {i}"},
            success=True,
        )
        assert not result["blocked"]
        assert not result["force_stop"]
        if i == 4:
            assert any("near-duplicate" in warning for warning in result["warnings"])

    blocked_precheck = tracker.precheck("web_search", {"query": f"{base_query} variant 8"})
    assert blocked_precheck.action == "block"
    assert blocked_precheck.reason == "near_duplicate_tool_calls"

    result = check_tool_warnings(
        tracker,
        "web_search",
        {"query": f"{base_query} variant 8"},
        success=True,
    )
    assert result["blocked"]
    assert result["force_stop"]
    assert result["blocked_info"]["scope"] == "near_duplicate_tool_calls"


def test_near_duplicate_guard_ignores_different_topics():
    tracker = ToolCallTracker()
    queries = [
        "python asyncio timeout semantics",
        "postgres vacuum full lock behavior",
        "react router splat future flag",
        "openai tool_choice none compatibility",
        "langfuse dataset run item api",
    ]
    for query in queries:
        result = check_tool_warnings(tracker, "web_search", {"query": query}, success=True)
        assert not result["blocked"]
        assert not result["force_stop"]


def test_near_duplicate_precheck_reserves_pending_signatures():
    tracker = ToolCallTracker()
    base_query = "ams frozen peas standard pdf issue effective current"

    for i in range(8):
        decision = tracker.precheck("web_search", {"query": f"{base_query} variant {i}"})
        assert decision.action == "allow"

    blocked = tracker.precheck("web_search", {"query": f"{base_query} variant 8"})
    assert blocked.action == "block"
    assert blocked.reason == "near_duplicate_tool_calls"

    for i in range(8):
        result = check_tool_warnings(
            tracker,
            "web_search",
            {"query": f"{base_query} variant {i}"},
            success=True,
        )
        assert not result["blocked"]
        assert not result["force_stop"]
        assert any("near-duplicate" in warning for warning in result["warnings"])

    tracker.clear_pending_near_duplicates()
    assert tracker.precheck("web_search", {"query": f"{base_query} variant 9"}).action == "block"


@pytest.mark.asyncio
async def test_concurrent_batch_blocks_near_duplicate_web_search_before_execution():
    agent = _make_loop_stub_agent()
    executed = []
    published = []

    async def fake_execute_tool_call(name, args):
        executed.append((name, args))
        return ToolExecutionResult(text="ok", status="ok", outcome="success")

    def fake_publish_tool_result_event(*args, **kwargs):
        published.append(kwargs)

    agent.execute_tool_call = fake_execute_tool_call
    agent.publish_tool_result_event = fake_publish_tool_result_event
    agent.append_tool_message = lambda *args, **kwargs: None

    loop = AgentLoop(agent)
    loop._auto_mark_bad_case = lambda *args, **kwargs: None

    base_query = "ams frozen peas standard pdf issue effective current"
    items = [
        {
            "tc": {"id": f"call_{i}"},
            "fn": "web_search",
            "inp": {"query": f"{base_query} variant {i}"},
            "allowed": True,
        }
        for i in range(9)
    ]

    guard_stop, guard_reason = await loop._execute_concurrent_batch(items)

    assert executed == []
    assert guard_stop is True
    assert guard_reason == "near_duplicate_tool_calls"
    blocked_outcomes = [event for event in published if event.get("outcome") == "blocked"]
    assert len(blocked_outcomes) == 9


def _make_loop_stub_agent():
    agent = SimpleNamespace(
        session=Session(session_id="loop-stub", origin="test"),
        _current_turn=1,
        _current_step=0,
        _tool_call_count=0,
        max_tool_calls=None,
        abort_requested=lambda: False,
        mark_last_usage_position=lambda seq: None,
        increment_turns=lambda: None,
        check_budget=lambda: {"exceeded": False},
        tool_budget_exceeded=lambda: False,
        clear_context_flag=lambda: None,
        refresh_runtime_system_prompt=lambda force=False: None,
    )

    def append_user_message(content, **kwargs):
        agent.session.append("user_message", {"content": content, **kwargs})

    agent.append_user_message = append_user_message
    return agent


def _tool_call_response():
    return {
        "choices": [{
            "message": {
                "content": "",
                "thinking": None,
                "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "web_search", "arguments": "{}"}}],
            },
            "finish_reason": "tool_calls",
        }],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


async def _stub_loop_prereqs(loop: AgentLoop):
    async def _noop(*args, **kwargs):
        return None

    loop._prepare_turn = _noop
    loop._consume_memory_prefetch = _noop
    loop._consume_wiki_prefetch = _noop
    loop._update_token_stats = lambda response: None


@pytest.mark.asyncio
async def test_agent_loop_breaks_after_loop_guard_force_stop():
    agent = _make_loop_stub_agent()
    loop = AgentLoop(agent)
    await _stub_loop_prereqs(loop)

    model_calls = 0

    async def fake_call_model_stream(*args, **kwargs):
        nonlocal model_calls
        model_calls += 1
        return _tool_call_response()

    handled = []
    finalized = []

    async def fake_handle_tool_calls(tool_calls):
        handled.append(tool_calls)
        return True

    loop.call_model_stream = fake_call_model_stream
    loop._handle_tool_calls = fake_handle_tool_calls
    loop._finalize_loop_guard_stop = lambda: finalized.append(True)

    await loop.run("test")

    assert model_calls == 1
    assert len(handled) == 1
    assert finalized == [True]


@pytest.mark.asyncio
async def test_agent_loop_finalizes_tool_budget():
    agent = _make_loop_stub_agent()
    agent.tool_budget_exceeded = lambda: True
    loop = AgentLoop(agent)
    await _stub_loop_prereqs(loop)

    model_calls = 0

    async def fake_call_model_stream(*args, **kwargs):
        nonlocal model_calls
        model_calls += 1
        return _tool_call_response()

    handled = []
    finalized = []

    async def fake_handle_tool_calls(tool_calls):
        handled.append(tool_calls)
        return False

    async def fake_finalize_tool_budget():
        finalized.append(True)

    loop.call_model_stream = fake_call_model_stream
    loop._handle_tool_calls = fake_handle_tool_calls
    loop._finalize_tool_budget = fake_finalize_tool_budget

    await loop.run("test")

    assert model_calls == 1
    assert len(handled) == 1
    assert finalized == [True]


@pytest.mark.asyncio
async def test_finalize_tool_budget_requests_summary_without_tools():
    agent = _make_loop_stub_agent()
    agent._tool_call_count = 5
    agent.max_tool_calls = 5
    agent._tool_budget_stop_reason = None
    loop = AgentLoop(agent)
    await _stub_loop_prereqs(loop)

    kwargs_seen = []

    async def fake_call_model_stream(*args, **kwargs):
        kwargs_seen.append(kwargs)
        return {
            "choices": [{
                "message": {"content": "partial summary", "thinking": None, "tool_calls": None},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
        }

    loop.call_model_stream = fake_call_model_stream
    await loop._finalize_tool_budget()

    assert kwargs_seen == [{"tools_enabled": False}]
    assert agent._tool_budget_stop_reason == "tool_budget_exceeded"
    event_types = [event["type"] for event in agent.session.events]
    assert "user_message" in event_types
    assert "assistant_message" in event_types


class BudgetFakeSubAgent(FakeSubAgent):
    def __init__(self):
        super().__init__(delay=0.01)
        self._tool_call_count = 5
        self._failed_tool_call_count = 1
        self._turn_number = 1

    async def run_once(self, prompt: str):
        await asyncio.sleep(self._delay)
        return {
            "text": "partial evidence",
            "tokens": {"input": 10, "output": 5},
            "stop_reason": "tool_budget_exceeded",
            "tool_budget_exceeded": True,
            "tool_call_count": self._tool_call_count,
            "failed_tool_call_count": self._failed_tool_call_count,
        }


class BudgetFakeAgent(FakeAgent):
    def __init__(self):
        super().__init__()
        self.spawn_kwargs = None

    def _spawn_sub_agent(self, **kwargs):
        self.spawn_kwargs = kwargs
        return BudgetFakeSubAgent()


@pytest.mark.asyncio
async def test_sub_agent_tool_budget_returns_partial_result(monkeypatch):
    monkeypatch.setattr("agents.core.subagent.get_sub_agent_config", lambda agent_type: {
        "system_prompt": "test",
        "tools": [],
        "model_ref": "",
        "max_tool_calls": 5,
    })

    agent = BudgetFakeAgent()
    dispatcher = ToolDispatcher(agent_ref=agent)
    result = await dispatcher._execute_agent_tool({"type": "explore", "prompt": "budget task"})

    assert agent.spawn_kwargs["max_tool_calls"] == 5
    assert isinstance(result, ToolExecutionResult)
    assert result.text == "partial evidence"
    assert result.status == "ok"
    assert result.outcome == "budget_exceeded"
    assert result.metadata["max_tool_calls"] == 5
    assert result.metadata["partial"] is True

    end_events = [e for e in agent.session.events if e.get("type") == "sub_agent/end"]
    assert len(end_events) == 1
    assert end_events[0]["status"] == "budget_exceeded"
    assert end_events[0]["outcome"] == "budget_exceeded"
    assert end_events[0]["max_tool_calls"] == 5
    assert end_events[0]["stop_reason"] == "tool_budget_exceeded"
    assert end_events[0]["tool_call_count"] == 5


@pytest.mark.asyncio
async def test_execute_tool_call_preserves_structured_result():
    class StructuredDispatcher(ToolDispatcher):
        async def _execute_tool_call_inner(self, name: str, inp: dict):
            return ToolExecutionResult(
                text="partial",
                status="ok",
                outcome="budget_exceeded",
                metadata={"reason": "tool_budget_exceeded"},
            )

    dispatcher = StructuredDispatcher(agent_ref=FakeAgent())
    result = await dispatcher.execute_tool_call("agent", {"type": "explore", "prompt": "task"})

    assert result.status == "ok"
    assert result.outcome == "budget_exceeded"
    assert result.metadata["reason"] == "tool_budget_exceeded"
    assert result.metadata["tool_name"] == "agent"


def test_sub_agent_max_tool_calls_configuration(monkeypatch):
    monkeypatch.delenv("MYCODE_SUB_AGENT_MAX_TOOL_CALLS", raising=False)
    monkeypatch.delenv("MYCODE_EXPLORE_MAX_TOOL_CALLS", raising=False)

    assert get_sub_agent_max_tool_calls("explore") == 80
    assert get_sub_agent_max_tool_calls("general") == 120

    monkeypatch.setenv("MYCODE_SUB_AGENT_MAX_TOOL_CALLS", "33")
    assert get_sub_agent_max_tool_calls("explore") == 33
    assert get_sub_agent_max_tool_calls("general") == 33

    monkeypatch.setenv("MYCODE_EXPLORE_MAX_TOOL_CALLS", "11")
    assert get_sub_agent_max_tool_calls("explore") == 11
    assert get_sub_agent_max_tool_calls("general") == 33
