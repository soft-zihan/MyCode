"""Step 2: abort propagation from parent to sub-agent via shared event."""

from __future__ import annotations

import asyncio

import pytest

from agents.agent import Agent


def _make_agent(**kwargs):
    defaults = dict(model="deepseek-chat", api_base="https://example.com/v1", api_key="sk-test")
    defaults.update(kwargs)
    return Agent(**defaults)


def test_abort_sets_event():
    agent = _make_agent()
    assert not agent._abort_event.is_set()
    agent.abort()
    assert agent._abort_event.is_set()
    assert agent._aborted is True


def test_abort_requested_reflects_own_state():
    agent = _make_agent()
    assert agent._abort_requested() is False
    agent.abort()
    assert agent._abort_requested() is True


def test_child_inherits_parent_abort_event():
    parent = _make_agent()
    child = _make_agent(parent_abort_event=parent._abort_event)
    # Child not aborted yet, parent not aborted yet.
    assert child._abort_requested() is False
    # Parent aborts -> child should observe it via the shared event.
    parent.abort()
    assert child._abort_requested() is True
    # Child's own _aborted flag is still False (it was not directly aborted).
    assert child._aborted is False


def test_chat_clears_abort_event_for_next_turn():
    agent = _make_agent()
    agent.abort()
    assert agent._abort_event.is_set()
    # Simulate what chat() does at the start of a new turn.
    agent._aborted = False
    agent._abort_event.clear()
    assert agent._abort_requested() is False


@pytest.mark.asyncio
async def test_parent_abort_stops_child_loop():
    """A running child loop should exit once the parent abort event is set."""
    parent = _make_agent()
    child = _make_agent(parent_abort_event=parent._abort_event)

    observed = {"iterations": 0}

    async def child_loop():
        # Mimic the agent loop's abort checkpoint.
        while True:
            if child._abort_requested():
                child._aborted = True
                break
            observed["iterations"] += 1
            await asyncio.sleep(0.01)

    task = asyncio.create_task(child_loop())
    await asyncio.sleep(0.03)  # let it spin a few iterations
    parent.abort()
    await asyncio.wait_for(task, timeout=1.0)
    assert child._aborted is True
    assert observed["iterations"] >= 1
