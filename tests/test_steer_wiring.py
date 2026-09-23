"""U1 steer 接线：MessageQueue 双队列语义、step 边界注入、终态丢弃审计。

run() 全链路（continue-on-follow_up）由 smoke chain 终验覆盖，本文件测
不依赖模型调用的接线单元：队列语义、_drain_steering/_drain_follow_up/
_drop_queued 的事件落盘与计数器重置、Agent.steer 委托。
"""

from __future__ import annotations

import pytest

from agents.core.steer_queue import MessageQueue


def _make_agent():
    from agents.agent import Agent
    return Agent(model="test-model", api_base="https://example.com/v1", api_key="sk-test")


# ─── MessageQueue 单元语义 ───────────────────────────────────────


@pytest.mark.asyncio
async def test_queue_drains_in_order_and_separates_queues():
    q = MessageQueue()
    await q.steer("a")
    await q.steer("b")
    await q.follow_up("c")
    assert await q.has_steering()
    assert await q.has_follow_up()
    steering = await q.drain_steering()
    assert [m.content for m in steering] == ["a", "b"]
    assert not await q.has_steering()
    follow = await q.drain_follow_up()
    assert [m.content for m in follow] == ["c"]
    assert not await q.has_follow_up()


@pytest.mark.asyncio
async def test_queue_clear_drops_both_queues():
    q = MessageQueue()
    await q.steer("x")
    await q.follow_up("y")
    await q.clear()
    assert q.steering_count == 0
    assert q.follow_up_count == 0


# ─── Agent.steer 委托 ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_agent_steer_writes_message_queue():
    agent = _make_agent()
    await agent.steer("换个方向")
    msgs = await agent.message_queue.drain_steering()
    assert [m.content for m in msgs] == ["换个方向"]


# ─── AgentLoop 注入/丢弃语义 ────────────────────────────────────


@pytest.mark.asyncio
async def test_drain_steering_injects_events_and_resets_counters():
    from agents.agent_loop import AgentLoop
    agent = _make_agent()
    loop = AgentLoop(agent)
    agent._tool_error_streak = 3
    agent._same_tool_repeat_count = 4
    agent._last_tool_name = "run_shell"
    await agent.message_queue.steer("别再用那个命令了")

    assert await loop._drain_steering() is True

    types = [e.get("type") for e in agent.session.events]
    assert "user_message" in types
    user_msgs = [e for e in agent.session.events if e.get("type") == "user_message"]
    assert user_msgs[-1]["content"] == "别再用那个命令了"
    delivered = [e for e in agent.session.events if e.get("type") == "steer/delivered"]
    assert len(delivered) == 1
    assert delivered[0]["queue"] == "steering"
    assert delivered[0]["source"] == "user"
    # 纠偏即新方向：连击计数重置
    assert agent._tool_error_streak == 0
    assert agent._same_tool_repeat_count == 0
    assert agent._last_tool_name == ""
    # 二次 drain 为空
    assert await loop._drain_steering() is False


@pytest.mark.asyncio
async def test_drain_follow_up_injects_with_queue_label():
    from agents.agent_loop import AgentLoop
    agent = _make_agent()
    loop = AgentLoop(agent)
    await agent.message_queue.follow_up("顺便跑下测试")

    assert await loop._drain_follow_up() is True
    delivered = [e for e in agent.session.events if e.get("type") == "steer/delivered"]
    assert delivered[0]["queue"] == "follow_up"
    user_msgs = [e for e in agent.session.events if e.get("type") == "user_message"]
    assert user_msgs[-1]["content"] == "顺便跑下测试"
    # follow_up 不重置连击计数（非纠偏语义）
    assert await loop._drain_follow_up() is False


@pytest.mark.asyncio
async def test_drop_queued_records_audit_event():
    from agents.agent_loop import AgentLoop
    agent = _make_agent()
    loop = AgentLoop(agent)
    await agent.message_queue.steer("残留消息")
    await agent.message_queue.follow_up("残留追问")

    await loop._drop_queued("aborted")

    dropped = [e for e in agent.session.events if e.get("type") == "steer/dropped"]
    assert len(dropped) == 1
    assert dropped[0]["reason"] == "aborted"
    assert dropped[0]["count"] == 2
    assert agent.message_queue.steering_count == 0
    assert agent.message_queue.follow_up_count == 0


@pytest.mark.asyncio
async def test_drop_queued_noop_when_empty():
    from agents.agent_loop import AgentLoop
    agent = _make_agent()
    loop = AgentLoop(agent)
    await loop._drop_queued("aborted")
    assert not [e for e in agent.session.events if e.get("type") == "steer/dropped"]


def test_steer_audit_events_not_rendered_to_llm_messages():
    """steer/queued、steer/delivered、steer/dropped 是审计事件，不得进 LLM 消息。"""
    from agents.core.session import derive_messages_from_event
    for t in ("steer/queued", "steer/delivered", "steer/dropped"):
        assert derive_messages_from_event({"type": t, "content": "x"}) == []
    # 注入的 user_message 正常渲染为 user 角色
    assert derive_messages_from_event({"type": "user_message", "content": "hi"}) == [
        {"role": "user", "content": "hi"}
    ]
