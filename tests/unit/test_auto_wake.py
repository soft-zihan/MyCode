"""U3b 自动唤醒：预算派生、request_auto_wake 守卫、唤醒轮事件语义。"""

from __future__ import annotations

import asyncio

import pytest

from agents.core.auto_wake import AUTO_WAKE_BUDGET, auto_wake_budget_exhausted


def _wake_start(turn: int) -> dict:
    return {"type": "turn/start", "turn": turn, "trigger": "auto_wake"}


def _user_start(turn: int) -> dict:
    return {"type": "turn/start", "turn": turn, "trigger": "user"}


class TestAutoWakeBudget:
    def test_empty_log_not_exhausted(self):
        assert not auto_wake_budget_exhausted([])

    def test_under_budget(self):
        events = [_user_start(1), {"type": "user_message"}, _wake_start(2)] * 1
        events += [_wake_start(3)]
        assert not auto_wake_budget_exhausted(events)

    def test_exhausted_at_budget(self):
        events = [{"type": "user_message"}] + [_wake_start(i) for i in range(AUTO_WAKE_BUDGET)]
        assert auto_wake_budget_exhausted(events)

    def test_user_message_resets_budget(self):
        events = [_wake_start(i) for i in range(AUTO_WAKE_BUDGET)]
        events += [{"type": "user_message"}, _wake_start(9)]
        assert not auto_wake_budget_exhausted(events)

    def test_user_trigger_turns_not_counted(self):
        events = [{"type": "user_message"}] + [_user_start(i) for i in range(10)]
        assert not auto_wake_budget_exhausted(events)

    def test_events_after_notification_still_counted(self):
        # subagent/completed 落在唤醒轮之间不重置预算
        events = [{"type": "user_message"}]
        events += [_wake_start(1), {"type": "subagent/completed"}, _wake_start(2), _wake_start(3)]
        assert auto_wake_budget_exhausted(events)


@pytest.fixture
def agent_env(tmp_path, monkeypatch):
    monkeypatch.setenv("MYCODE_SESSION_DIR", str(tmp_path / "sessions"))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    import agents.core.session as sess
    import agents.core.session_projection_cache as spc

    monkeypatch.setattr(sess, "_backend", None)
    monkeypatch.setattr(spc, "_projection_cache", None)
    ws = tmp_path / "ws"
    ws.mkdir()
    yield ws
    monkeypatch.setattr(sess, "_backend", None)
    monkeypatch.setattr(spc, "_projection_cache", None)


def _make_agent(agent_env, **kwargs):
    from agents.agent import Agent

    agent = Agent(
        model="m", api_key="sk-test", api_base="https://x.example/v1",
        workspace=agent_env, **kwargs,
    )
    agent._mcp_initialized = True  # 跳过 MCP 网络初始化
    agent._turn_number = 5  # 跳过首轮 wiki 待确认注入（读真实 HOME）
    return agent


class TestRequestAutoWake:
    @pytest.mark.asyncio
    async def test_idle_schedules_wake_turn(self, agent_env):
        agent = _make_agent(agent_env)
        woken: list[str] = []

        async def fake_wake_turn(reason):
            woken.append(reason)

        agent._turn_runner.wake_turn = fake_wake_turn
        assert agent.request_auto_wake(reason="subagent_completed") is True
        await asyncio.wait_for(agent._auto_wake_task, timeout=2)
        assert woken == ["subagent_completed"]

    @pytest.mark.asyncio
    async def test_busy_skips(self, agent_env):
        agent = _make_agent(agent_env)
        await agent._turn_lock.acquire()
        try:
            assert agent.request_auto_wake() is False
        finally:
            agent._turn_lock.release()

    @pytest.mark.asyncio
    async def test_aborted_skips(self, agent_env):
        agent = _make_agent(agent_env)
        agent.abort()
        assert agent.request_auto_wake() is False

    @pytest.mark.asyncio
    async def test_sub_agent_skips(self, agent_env):
        agent = _make_agent(agent_env, is_sub_agent=True)
        assert agent.request_auto_wake() is False

    @pytest.mark.asyncio
    async def test_budget_exhausted_skips(self, agent_env):
        agent = _make_agent(agent_env)
        agent.session.append("user_message", {"content": "hi"})
        for i in range(AUTO_WAKE_BUDGET):
            agent.session.append("turn/start", {"turn": i + 1, "trigger": "auto_wake"})
        assert agent.request_auto_wake() is False

    @pytest.mark.asyncio
    async def test_pending_wake_not_double_scheduled(self, agent_env):
        agent = _make_agent(agent_env)
        gate = asyncio.Event()

        async def slow_wake(reason):
            await gate.wait()

        agent._turn_runner.wake_turn = slow_wake
        assert agent.request_auto_wake() is True
        await asyncio.sleep(0)  # 让 task 进入执行
        assert agent.request_auto_wake() is False
        gate.set()
        await asyncio.wait_for(agent._auto_wake_task, timeout=2)

    @pytest.mark.asyncio
    async def test_abort_cancels_pending_wake(self, agent_env):
        agent = _make_agent(agent_env)
        gate = asyncio.Event()

        async def slow_wake(reason):
            await gate.wait()

        agent._turn_runner.wake_turn = slow_wake
        agent.request_auto_wake()
        await asyncio.sleep(0)
        agent.abort()
        with pytest.raises(asyncio.CancelledError):
            await agent._auto_wake_task


def _fake_model_call(reply: str):
    async def fake_call_model_stream(*, tools_enabled: bool = True, tool_choice: str | None = None):
        return {
            "choices": [{"message": {"content": reply, "tool_calls": None, "thinking": None}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }
    return fake_call_model_stream


class TestWakeTurnEvents:
    @pytest.mark.asyncio
    async def test_wake_turn_no_user_message_and_trigger_recorded(self, agent_env):
        agent = _make_agent(agent_env)
        agent._loop.call_model_stream = _fake_model_call("后台任务已完成：BG-COMPLETE-77")
        # synthetic 通知已在事件流（U3a 语义）
        agent.session.append("subagent/completed", {"notification_id": "n1", "text": "<subagent>done</subagent>"})

        await agent._turn_runner.run_turn(None, trigger="auto_wake")

        types = [e["type"] for e in agent.session.events]
        assert "user_message" not in types, "唤醒轮不得写 user_message 事件"
        starts = [e for e in agent.session.events if e["type"] == "turn/start"]
        assert len(starts) == 1 and starts[0]["trigger"] == "auto_wake"
        assert "assistant_message" in types and "turn/end" in types
        # 通知先于唤醒轮，模型经增量派生可见
        assert types.index("subagent/completed") < types.index("turn/start")

    @pytest.mark.asyncio
    async def test_user_turn_trigger_and_message_written(self, agent_env):
        agent = _make_agent(agent_env)
        agent._loop.call_model_stream = _fake_model_call("ok")
        agent.start_wiki_prefetch = lambda *a, **k: None  # 离线：不起 wiki 预取任务

        await agent._turn_runner.run_turn("你好")

        types = [e["type"] for e in agent.session.events]
        assert "user_message" in types
        starts = [e for e in agent.session.events if e["type"] == "turn/start"]
        assert starts[0]["trigger"] == "user"

    @pytest.mark.asyncio
    async def test_wake_turn_skips_when_lock_busy(self, agent_env):
        agent = _make_agent(agent_env)
        agent._loop.call_model_stream = _fake_model_call("should not run")
        await agent._turn_lock.acquire()
        try:
            await asyncio.wait_for(agent._turn_runner.run_turn(None, trigger="auto_wake"), timeout=1)
        finally:
            agent._turn_lock.release()
        assert not any(e["type"] == "turn/start" for e in agent.session.events)

    @pytest.mark.asyncio
    async def test_wake_turn_skips_behind_busy_user_turn(self, agent_env):
        """用户轮持锁时 wake_turn 直接跳过（不排队）——运行中轮次经增量派生自见通知。"""
        agent = _make_agent(agent_env)
        order: list[str] = []

        async def slow_model(*, tools_enabled=True, tool_choice=None):
            order.append("model-start")
            await asyncio.sleep(0.05)
            order.append("model-end")
            return {
                "choices": [{"message": {"content": "x", "tool_calls": None, "thinking": None}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }

        agent._loop.call_model_stream = slow_model
        user_task = asyncio.create_task(agent._turn_runner.run_turn("hi"))
        await asyncio.sleep(0.01)  # 用户轮先持锁
        await agent._turn_runner.wake_turn("test")
        await user_task
        assert order == ["model-start", "model-end"], "忙时唤醒轮必须跳过，不得并发/排队执行"
        triggers = [e.get("trigger") for e in agent.session.events if e["type"] == "turn/start"]
        assert triggers == ["user"]
