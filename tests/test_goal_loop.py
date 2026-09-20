"""Step 7: /goal autonomous loop with verifier and budget guardrails."""

from __future__ import annotations

import asyncio

import pytest

from agents.goal import (
    GoalLoop,
    GoalState,
    _extract_json,
    extract_goal_criteria,
)


class FakeAgent:
    """Minimal agent double for driving GoalLoop without a real model."""

    def __init__(self, reports):
        # reports: list of assistant texts returned one per chat() call.
        self._reports = list(reports)
        self.prompts = []
        self._aborted = False
        self._abort_event = asyncio.Event()
        self._last_assistant_text = ""

    async def chat(self, prompt):
        self.prompts.append(prompt)
        self._last_assistant_text = self._reports.pop(0) if self._reports else "(done)"

    def _build_side_query(self, max_tokens=0):
        return None  # tests inject side_query explicitly


def _verifier(responses):
    """Build a side_query that returns verifier JSON responses in order."""
    it = iter(responses)

    async def side_query(system, user):
        return next(it)

    return side_query


# ─── helpers ────────────────────────────────────────────────

def test_extract_json_parses_object():
    assert _extract_json('{"a": 1}') == {"a": 1}
    assert _extract_json('noise {"a": 1} more') == {"a": 1}
    assert _extract_json("no json here") is None
    assert _extract_json(None) is None


# ─── criteria extraction ────────────────────────────────────

@pytest.mark.asyncio
async def test_extract_goal_criteria_parses():
    async def sq(system, user):
        return '{"criteria": ["file exists", "tests pass"]}'

    criteria = await extract_goal_criteria("make it work", sq)
    assert criteria == ["file exists", "tests pass"]


@pytest.mark.asyncio
async def test_extract_goal_criteria_failure_returns_empty():
    async def sq(system, user):
        raise RuntimeError("boom")

    assert await extract_goal_criteria("x", sq) == []


@pytest.mark.asyncio
async def test_extract_goal_criteria_bad_json_returns_empty():
    async def sq(system, user):
        return "I cannot produce JSON."

    assert await extract_goal_criteria("x", sq) == []


# ─── GoalLoop ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_goal_achieved_first_iteration():
    agent = FakeAgent(["I created the file and tests pass."])
    sq = _verifier(['{"criteria": [{"id": 1, "met": true, "evidence": "ok"}], "all_met": true}'])
    loop = GoalLoop(agent, "goal", ["c1"], side_query=sq)
    state = await loop.run()
    assert state.status == "achieved"
    assert state.iteration == 1
    assert len(agent.prompts) == 1


@pytest.mark.asyncio
async def test_goal_feedback_fed_back_then_achieved():
    agent = FakeAgent(["attempt 1", "attempt 2"])
    sq = _verifier([
        '{"criteria": [{"id": 1, "met": false, "evidence": "missing"}], "all_met": false}',
        '{"criteria": [{"id": 1, "met": true, "evidence": "ok"}], "all_met": true}',
    ])
    loop = GoalLoop(agent, "goal", ["c1"], side_query=sq)
    state = await loop.run()
    assert state.status == "achieved"
    assert state.iteration == 2
    # Second prompt must carry the unmet feedback.
    assert "NOT MET" in agent.prompts[1]


@pytest.mark.asyncio
async def test_goal_budget_exhausted():
    agent = FakeAgent(["a", "b", "c"])
    sq = _verifier([
        '{"criteria": [{"id": 1, "met": false, "evidence": "no"}], "all_met": false}',
        '{"criteria": [{"id": 1, "met": false, "evidence": "no"}], "all_met": false}',
    ])
    loop = GoalLoop(agent, "goal", ["c1"], side_query=sq, max_iterations=2)
    state = await loop.run()
    assert state.status == "budget_exhausted"
    assert state.iteration == 2


@pytest.mark.asyncio
async def test_goal_aborted_before_start():
    agent = FakeAgent(["x"])
    agent._abort_event.set()
    sq = _verifier(['{"criteria": [], "all_met": true}'])
    loop = GoalLoop(agent, "goal", ["c1"], side_query=sq)
    state = await loop.run()
    assert state.status == "aborted"
    assert state.iteration == 0


@pytest.mark.asyncio
async def test_goal_unparseable_verifier_continues():
    agent = FakeAgent(["a", "b"])
    sq = _verifier([
        "sorry, cannot decide",
        '{"criteria": [{"id": 1, "met": true, "evidence": "ok"}], "all_met": true}',
    ])
    loop = GoalLoop(agent, "goal", ["c1"], side_query=sq, max_iterations=3)
    state = await loop.run()
    assert state.status == "achieved"
    assert state.iteration == 2


# ─── GoalState serialization ────────────────────────────────

def test_goal_state_roundtrip():
    s = GoalState(goal="g", criteria=["a", "b"], iteration=3, status="achieved", last_feedback="fb")
    d = s.to_dict()
    s2 = GoalState.from_dict(d)
    assert s2.goal == "g"
    assert s2.criteria == ["a", "b"]
    assert s2.iteration == 3
    assert s2.status == "achieved"
    assert s2.last_feedback == "fb"
