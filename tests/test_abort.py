"""Step 1: Ctrl+C must abort a running agent turn.

Regression: the REPL SIGINT handler used `agent._output_buffer is not None`
to detect "agent is processing", but `_output_buffer` is only set inside
`run_once()` (sub-agents). During normal REPL chat the condition was always
False, so Ctrl+C never aborted — it only warned/exited.
"""

from __future__ import annotations

from agents.main import _should_abort_on_sigint


class FakeAgent:
    """Duck-typed stand-in exposing only what the SIGINT check reads."""

    def __init__(self, *, aborted: bool, processing: bool):
        self._aborted = aborted
        self.is_processing = processing


def test_sigint_aborts_running_chat():
    agent = FakeAgent(aborted=False, processing=True)
    assert _should_abort_on_sigint(agent) is True


def test_sigint_does_not_abort_when_idle():
    agent = FakeAgent(aborted=False, processing=False)
    assert _should_abort_on_sigint(agent) is False


def test_sigint_does_not_double_abort():
    agent = FakeAgent(aborted=True, processing=True)
    assert _should_abort_on_sigint(agent) is False


def test_real_agent_is_processing_reflects_task_state():
    """Sanity-check the real Agent.is_processing property contract."""
    from agents.agent import Agent

    agent = Agent(model="deepseek-chat", api_base="https://example.com/v1", api_key="sk-test")
    assert agent.is_processing is False  # no task running yet
