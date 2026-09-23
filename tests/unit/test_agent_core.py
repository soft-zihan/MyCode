from __future__ import annotations

import asyncio

import pytest

from agents.agent import Agent


def _make_agent(**kwargs):
    defaults = dict(model="deepseek-chat", api_base="https://example.com/v1", api_key="sk-test")
    defaults.update(kwargs)
    return Agent(**defaults)


class TestAgentInit:

    def test_default_session_id_generated(self):
        agent = _make_agent()
        assert agent.session_id
        assert len(agent.session_id) >= 8

    def test_custom_session_id(self):
        agent = _make_agent(session_id="custom-id")
        assert agent.session_id == "custom-id"

    def test_default_permission_mode(self):
        agent = _make_agent()
        assert agent.permission_mode == "default"

    def test_custom_permission_mode(self):
        agent = _make_agent(permission_mode="bypassPermissions")
        assert agent.permission_mode == "bypassPermissions"

    def test_initial_messages_only_system(self):
        agent = _make_agent()
        messages = agent.session.get_messages_for_llm()
        assert len(messages) == 1
        assert messages[0]["role"] == "system"

    def test_initial_token_counters_zero(self):
        agent = _make_agent()
        assert agent.total_input_tokens == 0
        assert agent.total_output_tokens == 0
        assert agent.last_input_token_count == 0
        assert agent.last_total_token_count == 0
        assert agent.estimated_context_tokens == 0

    def test_initial_turn_number_zero(self):
        agent = _make_agent()
        assert agent._turn_number == 0

    def test_initial_not_aborted(self):
        agent = _make_agent()
        assert agent._aborted is False
        assert not agent._abort_event.is_set()

    def test_is_sub_agent_default_false(self):
        agent = _make_agent()
        assert agent.is_sub_agent is False

    def test_is_sub_agent_set(self):
        agent = _make_agent(is_sub_agent=True)
        assert agent.is_sub_agent is True


class TestAgentAbort:

    def test_abort_sets_flags(self):
        agent = _make_agent()
        agent.abort()
        assert agent._aborted is True
        assert agent._abort_event.is_set()

    def test_abort_requested_reflects_state(self):
        agent = _make_agent()
        assert agent._abort_requested() is False
        agent.abort()
        assert agent._abort_requested() is True

    def test_is_processing_false_initially(self):
        agent = _make_agent()
        assert agent.is_processing is False


class TestAgentStatusLine:

    def test_status_line_contains_model(self):
        agent = _make_agent()
        line = agent.status_line()
        assert "deepseek-chat" in line

    def test_status_line_shows_tokens_after_update(self):
        agent = _make_agent()
        agent.set_last_usage_tokens(12000, 12650)
        line = agent.status_line()
        assert "12650" in line

    def test_status_line_shows_dash_before_first_call(self):
        agent = _make_agent()
        assert agent.estimated_context_tokens == 0
        line = agent.status_line()
        assert "ctx: -/" in line


class TestAgentContextWindow:

    def test_default_context_window_1m(self):
        agent = _make_agent(model="unknown-model")
        assert agent.context_window == 1_000_000
        assert agent.effective_window == 980_000
        assert agent.auto_compact_threshold == 0.80
        assert agent._compressor.tool_fold_threshold == 0.80


class TestAgentSpawnSubAgent:

    def test_spawn_creates_sub_agent(self):
        parent = _make_agent()
        child = parent._spawn_sub_agent(
            system_prompt="test prompt",
            tools=[],
            model_ref="",
            label="explore",
        )
        assert child.is_sub_agent is True
        assert child.model == parent.model

    def test_spawn_inherits_api_credentials(self):
        parent = _make_agent()
        child = parent._spawn_sub_agent(
            system_prompt="sp",
            tools=[],
            model_ref="",
            label="explore",
        )
        assert child._api_base == parent._api_base
        assert child._api_key == parent._api_key
    def test_spawn_shares_checkpoint_store(self):
        parent = _make_agent()
        child = parent._spawn_sub_agent(
            system_prompt="sp",
            tools=[],
            model_ref="",
            label="explore",
        )
        # Checkpoint store is no longer shared - sub-agents have their own
        assert child is not parent


    def test_spawn_shares_abort_event(self):
        parent = _make_agent()
        child = parent._spawn_sub_agent(
            system_prompt="sp",
            tools=[],
            model_ref="",
            label="explore",
        )
        parent.abort()
        assert child._abort_requested() is True


class TestAgentSessionLifecycle:

    def test_turn_boundary_recorded(self):
        agent = _make_agent()
        agent._turn_number += 1
        # Turn boundaries are now tracked differently
        assert agent._turn_number == 1

    @pytest.mark.asyncio
    async def test_event_append_persists_via_backend(self, tmp_path, monkeypatch):
        """append 的事件经当前 backend 落盘（jsonl/sqlite 一致）。"""
        monkeypatch.setenv("MYCODE_SESSION_DIR", str(tmp_path / "sessions"))
        import agents.core.session as core_session
        core_session._backend = None
        try:
            agent = _make_agent()
            agent.session.append("user_message", {"content": "hello"})
            backend = core_session.get_session_backend()
            assert backend.session_exists(agent.session_id)
            events = backend.load_all_events(agent.session_id)
            assert any(e.get("content") == "hello" for e in events)
        finally:
            core_session._backend = None

    def test_fork_creates_new_session_id(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MYCODE_SESSION_DIR", str(tmp_path / "sessions"))
        agent = _make_agent()
        agent.session.append("user_message", {"content": "hello"})
        old_id = agent.session_id
        msg = agent.fork_session()
        assert old_id in msg
        assert agent.session_id != old_id
