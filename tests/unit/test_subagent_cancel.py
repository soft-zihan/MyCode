"""U4 硬中止后台子代理：subagent_cancel 工具、cancel 端点、状态映射、D4 投影修复。"""

from __future__ import annotations

import asyncio
import time

import pytest

from agents.core.job_registry import SubAgentJob, get_job
from agents.core.session import Session
from agents.core.steer_queue import MessageQueue
from agents.core.subagent_runner import execute_agent_tool, execute_subagent_cancel_tool
from agents.tools.result import ToolExecutionResult


@pytest.fixture
def session_env(tmp_path, monkeypatch):
    monkeypatch.setenv("MYCODE_SESSION_DIR", str(tmp_path))
    import agents.core.session as sess
    import agents.core.session_projection_cache as spc
    import agents.core.job_registry as jr

    monkeypatch.setattr(sess, "_backend", None)
    monkeypatch.setattr(spc, "_projection_cache", None)
    jr.clear_jobs()
    yield tmp_path
    monkeypatch.setattr(sess, "_backend", None)
    monkeypatch.setattr(spc, "_projection_cache", None)
    jr.clear_jobs()


@pytest.fixture
def sub_agent_config(monkeypatch):
    monkeypatch.setattr("agents.core.subagent.get_sub_agent_config", lambda agent_type: {
        "system_prompt": "test",
        "tools": [],
        "model_ref": "",
        "max_tool_calls": 10,
    })


class CancelFakeSubAgent:
    def __init__(self, delay: float = 5.0):
        self.session = None
        self.session_id = None
        self._current_sub_agent_id = None
        self._aborted = False
        self._tool_call_count = 0
        self._failed_tool_call_count = 0
        self._turn_number = 0
        self.message_queue = MessageQueue()
        self._delay = delay
        self.finished = False
        self.cancelled = False
        self.abort_called = False

    def abort(self):
        self.abort_called = True
        self._aborted = True

    async def run_once(self, prompt: str):
        try:
            await asyncio.sleep(self._delay)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        self.finished = True
        self.session.append("assistant_message", {"content": f"run:{prompt}"})
        return {"text": "done", "tokens": {"input": 1, "output": 1}}


class CancelFakeAgent:
    def __init__(self, session_id: str = "parent-cancel", sub_delay: float = 5.0):
        self.session = Session(session_id=session_id)
        self.session_id = self.session.id
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.current_sub_agent_id = None
        self.permission_mode = "bypassPermissions"
        self.spawned: list[CancelFakeSubAgent] = []
        self._sub_delay = sub_delay

    def abort_requested(self) -> bool:
        return False

    def _spawn_sub_agent(self, **kwargs):
        sub = CancelFakeSubAgent(delay=self._sub_delay)
        self.spawned.append(sub)
        return sub


async def _launch_background(agent: CancelFakeAgent) -> str:
    result = await execute_agent_tool(
        agent,
        {"type": "general", "prompt": "long task", "description": "d", "background": True},
        timeout_s=30,
    )
    return result.metadata["sub_session_id"]


@pytest.mark.asyncio
async def test_cancel_tool_stops_background_job_fast_and_notifies(session_env, sub_agent_config):
    agent = CancelFakeAgent()
    sub_id = await _launch_background(agent)
    job = get_job(sub_id)
    assert job is not None

    t0 = time.time()
    result = await execute_subagent_cancel_tool(agent, {"session_id": sub_id})
    elapsed = time.time() - t0

    # 验收：cancel 后子代理 1s 内停止
    assert elapsed < 1.0, f"cancel 应在 1s 内完成收尾，实际 {elapsed:.2f}s"
    assert isinstance(result, ToolExecutionResult)
    assert 'state="cancelled"' in result.text
    assert result.outcome == "cancelled"

    # 子代理被硬停（未自然完成），软 abort + 硬 cancel 双通道都触达
    child = agent.spawned[0]
    assert not child.finished
    assert child.abort_called

    # 注册表清理 + 幂等取消通知落父会话（带 cancelled 标签）
    assert get_job(sub_id) is None
    assert job.cancel_requested
    notes = [e for e in agent.session.events if e["type"] == "subagent/completed"]
    assert len(notes) == 1
    assert notes[0]["status"] == "cancelled"
    assert 'state="cancelled"' in notes[0]["text"]
    # 审计事件 + sub_agent/end 落账
    assert any(e["type"] == "sub_agent/cancel" and e["sub_session_id"] == sub_id for e in agent.session.events)
    ends = [e for e in agent.session.events if e["type"] == "sub_agent/end"]
    assert len(ends) == 1 and ends[0]["status"] == "cancelled"


@pytest.mark.asyncio
async def test_cancel_tool_validation_errors(session_env, sub_agent_config):
    agent = CancelFakeAgent()
    sub_id = await _launch_background(agent)

    # 缺 session_id
    r = await execute_subagent_cancel_tool(agent, {})
    assert r.outcome == "error" and r.metadata["reason"] == "missing_session_id"
    # 非本会话子代理（防跨会话取消）
    r = await execute_subagent_cancel_tool(agent, {"session_id": "ghost-session"})
    assert r.outcome == "error" and r.metadata["reason"] == "cancel_not_owned"
    # 已结束/不存在
    r = await execute_subagent_cancel_tool(agent, {"session_id": sub_id})
    assert r.outcome == "cancelled"  # 第一次真取消
    r = await execute_subagent_cancel_tool(agent, {"session_id": sub_id})
    assert r.outcome == "error" and r.metadata["reason"] == "cancel_not_running"
    # 通知仍幂等（只有一条）
    notes = [e for e in agent.session.events if e["type"] == "subagent/completed"]
    assert len(notes) == 1


@pytest.mark.asyncio
async def test_cancel_maps_to_cancelled_not_timeout(session_env, sub_agent_config):
    """U4 语义修复：job 级取消映射为 cancelled——旧行为会把非父 abort 的
    CancelledError 一律解释成 timeout，取消被误报为超时。"""
    agent = CancelFakeAgent()
    sub_id = await _launch_background(agent)
    job = get_job(sub_id)
    job.request_cancel()
    await asyncio.wait_for(job.done.wait(), timeout=2)
    assert job.state["status"] == "cancelled"
    assert job.state["outcome"] == "cancelled"
    assert "timeout" not in job.state["summary"].lower()


@pytest.mark.asyncio
async def test_cancel_endpoint(session_env):
    """HTTP cancel 端点：归属校验 + 运行中校验 + 双通道触达。"""
    from fastapi import HTTPException
    from frontend.server.routers.sessions import api_cancel_subagent

    agent = CancelFakeAgent(session_id="parent-ep4")
    child = CancelFakeSubAgent()
    job = SubAgentJob(
        sub_session_id="s-c4", parent_session_id="parent-ep4",
        agent_type="general", description="d", sub_agent=child,
    )
    from agents.core import job_registry as jr
    jr.register_job(job)

    with pytest.raises(HTTPException):
        await api_cancel_subagent("other-session", "s-c4")
    out = await api_cancel_subagent("parent-ep4", "s-c4")
    assert out["success"] is True
    assert job.cancel_requested and child.abort_called
    job.done.set()
    out = await api_cancel_subagent("parent-ep4", "s-c4")
    assert out["success"] is False
    out = await api_cancel_subagent("parent-ep4", "ghost")
    assert out["success"] is False
    jr.pop_job("s-c4")


def test_d4_running_projection_honors_turn_cancel():
    """D4 修复：running 投影认 turn/cancel——否则被中止会话永久"运行中"。"""
    from agents.core.session_projection_cache import get_projection_registry

    reg = get_projection_registry()
    events = [
        {"seq": 1, "type": "turn/start"},
        {"seq": 2, "type": "turn/cancel", "reason": "user_abort"},
    ]
    state = reg.fold_events(reg.init_state(), events)
    assert state["running"] is False

    events_ok = [{"seq": 1, "type": "turn/start"}, {"seq": 2, "type": "turn/end"}]
    assert reg.fold_events(reg.init_state(), events_ok)["running"] is False
    assert reg.fold_events(reg.init_state(), [{"seq": 1, "type": "turn/start"}])["running"] is True
