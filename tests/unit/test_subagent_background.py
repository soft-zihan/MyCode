"""U3a 后台子代理：background 发起、前台转后台、synthetic 完成通知（幂等）、级联取消。"""

from __future__ import annotations

import asyncio
import time

import pytest

from agents.core.job_registry import SubAgentJob, get_job
from agents.core.session import Session, derive_messages_from_event
from agents.core.steer_queue import MessageQueue
from agents.core.subagent_runner import _fire_completion_notification, execute_agent_tool
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


class BgFakeSubAgent:
    def __init__(self, delay: float = 0.01):
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

    async def run_once(self, prompt: str):
        try:
            await asyncio.sleep(self._delay)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        self.finished = True
        n_prev = len(self.session.events) if self.session else 0
        self.session.append("assistant_message", {"content": f"run:{prompt}"})
        return {"text": f"prev={n_prev}", "tokens": {"input": 1, "output": 1}}


class BgFakeAgent:
    def __init__(self, session_id: str = "parent-bg", sub_delay: float = 0.01):
        self.session = Session(session_id=session_id)
        self.session_id = self.session.id
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.current_sub_agent_id = None
        self.permission_mode = "bypassPermissions"
        self.spawned: list[BgFakeSubAgent] = []
        self._sub_delay = sub_delay

    def abort_requested(self) -> bool:
        return False

    def _spawn_sub_agent(self, **kwargs):
        sub = BgFakeSubAgent(delay=self._sub_delay)
        self.spawned.append(sub)
        return sub


@pytest.mark.asyncio
async def test_background_launch_returns_immediately_and_notifies(session_env, sub_agent_config):
    agent = BgFakeAgent(sub_delay=0.3)
    t0 = time.time()
    result = await execute_agent_tool(
        agent,
        {"type": "general", "prompt": "bg task", "description": "d", "background": True},
        timeout_s=30,
    )
    elapsed = time.time() - t0
    assert elapsed < 0.2, f"background 发起应立即返回，实际 {elapsed:.2f}s"

    sub_id = result.metadata["sub_session_id"]
    assert result.metadata["status"] == "running"
    assert 'state="running"' in result.text
    assert "Do NOT sleep, poll" in result.text  # 反轮询文案在工具结果处（双处之一）

    job = get_job(sub_id)
    assert job is not None and job.backgrounded.is_set()
    await asyncio.wait_for(job.done.wait(), timeout=3)

    # 完成通知：synthetic 事件落父会话，带标签文本 + 预分配幂等 ID
    notes = [e for e in agent.session.events if e["type"] == "subagent/completed"]
    assert len(notes) == 1
    assert notes[0]["sub_session_id"] == sub_id
    assert notes[0]["notification_id"] == job.notification_id
    assert notes[0]["status"] == "completed"
    assert 'state="completed"' in notes[0]["text"] and "prev=1" in notes[0]["text"]

    # 注册表已弹出；sub_agent/end 照常落账
    assert get_job(sub_id) is None
    assert any(
        e["type"] == "sub_agent/end" and e["status"] == "completed" and e["sub_session_id"] == sub_id
        for e in agent.session.events
    )

    # 投影为 user 消息：下一轮父模型自动可见
    msgs = derive_messages_from_event(notes[0])
    assert msgs == [{"role": "user", "content": notes[0]["text"]}]


@pytest.mark.asyncio
async def test_completion_notification_idempotent(session_env):
    agent = BgFakeAgent()
    job = SubAgentJob(
        sub_session_id="s1", parent_session_id=agent.session_id,
        agent_type="general", description="d", sub_agent=None,
    )
    job.mark_background()
    job.state = {
        "status": "completed", "outcome": "success", "summary": "ok", "output_text": "done",
        "input_tokens": 0, "output_tokens": 0, "stop_reason": None, "error": None,
    }
    job.result = ToolExecutionResult(
        text='<subagent session_id="s1" state="completed">done</subagent>',
        status="ok", outcome="success", metadata={},
    )
    _fire_completion_notification(agent, job)
    _fire_completion_notification(agent, job)  # done/cancel 竞态双触发
    notes = [e for e in agent.session.events if e["type"] == "subagent/completed"]
    assert len(notes) == 1


@pytest.mark.asyncio
async def test_foreground_converts_to_background_midrun(session_env, sub_agent_config):
    agent = BgFakeAgent(sub_delay=0.3)
    fg = asyncio.create_task(execute_agent_tool(
        agent, {"type": "general", "prompt": "slow", "description": "d"}, timeout_s=30,
    ))
    await asyncio.sleep(0.05)
    starts = [e for e in agent.session.events if e["type"] == "sub_agent/start"]
    sub_id = starts[0]["sub_session_id"]
    job = get_job(sub_id)
    assert job is not None

    job.mark_background()  # 等价于 HTTP 转后台端点的核心动作
    result = await asyncio.wait_for(fg, timeout=1)
    assert 'state="backgrounded"' in result.text
    assert result.metadata["status"] == "backgrounded"

    # run 不中断，完成通知照常送达
    await asyncio.wait_for(job.done.wait(), timeout=3)
    assert agent.spawned[0].finished
    notes = [e for e in agent.session.events if e["type"] == "subagent/completed"]
    assert len(notes) == 1 and 'state="completed"' in notes[0]["text"]


@pytest.mark.asyncio
async def test_foreground_normal_completion_has_no_notification(session_env, sub_agent_config):
    agent = BgFakeAgent()
    result = await execute_agent_tool(
        agent, {"type": "general", "prompt": "fast", "description": "d"}, timeout_s=30,
    )
    assert 'state="completed"' in result.text
    # 前台正常完成：结果直接返回模型，不注入 synthetic 通知（防重复）
    assert not any(e["type"] == "subagent/completed" for e in agent.session.events)


@pytest.mark.asyncio
async def test_foreground_cancel_kills_run_without_notification(session_env, sub_agent_config):
    agent = BgFakeAgent(sub_delay=0.3)
    fg = asyncio.create_task(execute_agent_tool(
        agent, {"type": "general", "prompt": "slow", "description": "d"}, timeout_s=30,
    ))
    await asyncio.sleep(0.05)
    sub_id = [e for e in agent.session.events if e["type"] == "sub_agent/start"][0]["sub_session_id"]
    job = get_job(sub_id)

    fg.cancel()  # 模拟父级 abort/dispatcher 超时兜底的硬取消
    with pytest.raises(asyncio.CancelledError):
        await fg
    await asyncio.sleep(0.05)

    # 未后台化：级联杀 run（旧语义保持），不发完成通知
    assert agent.spawned[0].cancelled and not agent.spawned[0].finished
    assert not any(e["type"] == "subagent/completed" for e in agent.session.events)
    # 收尾仍完整：done 已置位、注册表弹出、sub_agent/end 落账
    assert job.done.is_set()
    assert get_job(sub_id) is None
    ends = [e for e in agent.session.events if e["type"] == "sub_agent/end"]
    assert len(ends) == 1 and ends[0]["sub_session_id"] == sub_id


@pytest.mark.asyncio
async def test_background_endpoint(session_env):
    """HTTP 转后台端点：归属校验 + 运行中校验 + 标记生效。"""
    from fastapi import HTTPException
    from frontend.server.routers.sessions import api_background_subagent

    agent = BgFakeAgent(session_id="parent-ep")
    job = SubAgentJob(
        sub_session_id="s-ep", parent_session_id="parent-ep",
        agent_type="general", description="d", sub_agent=None,
    )
    from agents.core import job_registry as jr
    jr.register_job(job)

    # 非本会话子代理 → 404
    with pytest.raises(HTTPException):
        await api_background_subagent("other-session", "s-ep")
    # 正常运行中 → 标记成功
    out = await api_background_subagent("parent-ep", "s-ep")
    assert out["success"] is True and job.backgrounded.is_set()
    # done 后 → 拒绝
    job.done.set()
    out = await api_background_subagent("parent-ep", "s-ep")
    assert out["success"] is False
    # 不存在 → 拒绝
    out = await api_background_subagent("parent-ep", "ghost")
    assert out["success"] is False
    jr.pop_job("s-ep")
