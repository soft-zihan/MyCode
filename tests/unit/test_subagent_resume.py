"""U2 可续跑子代理：归属校验、事件流恢复、steer join、派生会话列表过滤。"""

from __future__ import annotations

import asyncio

import pytest

from agents.core.session import Session
from agents.core.steer_queue import MessageQueue
from agents.core.job_registry import get_job
from agents.core.subagent_runner import execute_agent_tool


@pytest.fixture
def session_env(tmp_path, monkeypatch):
    monkeypatch.setenv("MYCODE_SESSION_DIR", str(tmp_path))
    import agents.core.session as sess
    import agents.core.session_projection_cache as spc

    monkeypatch.setattr(sess, "_backend", None)
    monkeypatch.setattr(spc, "_projection_cache", None)
    yield tmp_path
    monkeypatch.setattr(sess, "_backend", None)
    monkeypatch.setattr(spc, "_projection_cache", None)


@pytest.fixture
def sub_agent_config(monkeypatch):
    monkeypatch.setattr("agents.core.subagent.get_sub_agent_config", lambda agent_type: {
        "system_prompt": "test",
        "tools": [],
        "model_ref": "",
        "max_tool_calls": 10,
    })


class ResumeFakeSubAgent:
    """run_once 把'看到的历史事件数'回报出来，用于断言记忆连续性。"""

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

    async def run_once(self, prompt: str):
        await asyncio.sleep(self._delay)
        n_prev = len(self.session.events) if self.session else 0
        self.session.append("assistant_message", {"content": f"run:{prompt}"})
        return {"text": f"prev={n_prev}", "tokens": {"input": 1, "output": 1}}


class ResumeFakeAgent:
    def __init__(self, session_id: str = "parent-resume", sub_delay: float = 0.01):
        self.session = Session(session_id=session_id)
        self.session_id = self.session.id
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.current_sub_agent_id = None
        self.permission_mode = "bypassPermissions"
        self.spawned: list[ResumeFakeSubAgent] = []
        self._sub_delay = sub_delay

    def abort_requested(self) -> bool:
        return False

    def _spawn_sub_agent(self, **kwargs):
        sub = ResumeFakeSubAgent(delay=self._sub_delay)
        self.spawned.append(sub)
        return sub


@pytest.mark.asyncio
async def test_resume_rejects_foreign_session_id(session_env, sub_agent_config):
    agent = ResumeFakeAgent()
    result = await execute_agent_tool(
        agent, {"prompt": "hi", "session_id": "deadbeef"}, timeout_s=30,
    )
    assert result.status == "error"
    assert result.outcome == "error"
    assert result.metadata["reason"] == "resume_not_owned"


@pytest.mark.asyncio
async def test_resume_owned_but_no_events_returns_not_found(session_env, sub_agent_config):
    agent = ResumeFakeAgent()
    agent.session.append("sub_agent/start", {
        "agent_id": "ghost1", "agent_type": "general",
        "description": "d", "sub_session_id": "ghost1",
    })
    result = await execute_agent_tool(
        agent, {"prompt": "x", "session_id": "ghost1"}, timeout_s=30,
    )
    assert result.status == "error"
    assert result.metadata["reason"] == "resume_not_found"


@pytest.mark.asyncio
async def test_spawn_then_resume_restores_history(session_env, sub_agent_config):
    agent = ResumeFakeAgent()

    first = await execute_agent_tool(
        agent, {"type": "general", "prompt": "remember 42", "description": "t"}, timeout_s=30,
    )
    sub_id = first.metadata["sub_session_id"]
    assert first.outcome == "success"
    assert first.text == f'<subagent session_id="{sub_id}" state="completed">prev=1</subagent>'

    # 子会话已落盘且归属元数据可恢复
    loaded = Session.load_from_events(sub_id)
    assert loaded is not None
    assert loaded.origin == "sub_agent"
    assert loaded.parent_session == agent.session_id
    assert loaded.agent_type == "general"

    second = await execute_agent_tool(
        agent, {"prompt": "what was the number?", "session_id": sub_id}, timeout_s=30,
    )
    # 续跑看到的历史 = session/meta + 第一次 run 的 assistant_message（+父侧不计）→ prev=2
    assert second.outcome == "success"
    assert second.text == f'<subagent session_id="{sub_id}" state="completed">prev=2</subagent>'
    assert second.metadata["sub_session_id"] == sub_id

    resume_events = [e for e in agent.session.events if e.get("type") == "sub_agent/resume"]
    assert len(resume_events) == 1
    assert resume_events[0]["mode"] == "turn"
    assert resume_events[0]["sub_session_id"] == sub_id
    # 续跑复用同一子会话（append 续 seq，不新开）
    assert agent.spawned[1].session.id == sub_id
    assert get_job(sub_id) is None


@pytest.mark.asyncio
async def test_resume_joins_running_sub_agent_via_steer(session_env, sub_agent_config):
    agent = ResumeFakeAgent(sub_delay=0.3)

    spawn_task = asyncio.create_task(execute_agent_tool(
        agent, {"type": "general", "prompt": "slow task", "description": "d"}, timeout_s=30,
    ))
    await asyncio.sleep(0.05)
    start_events = [e for e in agent.session.events if e.get("type") == "sub_agent/start"]
    assert len(start_events) == 1
    sub_id = start_events[0]["sub_session_id"]

    join_task = asyncio.create_task(execute_agent_tool(
        agent, {"prompt": "also collect evidence", "session_id": sub_id}, timeout_s=30,
    ))
    first_result, join_result = await asyncio.gather(spawn_task, join_task)

    # join 方经 steer 队列注入，不新起 run，等待原 run 结果
    sub = agent.spawned[0]
    assert sub.message_queue.steering_count == 1
    queued = await sub.message_queue.drain_steering()
    assert queued[0].content == "also collect evidence"
    assert len(agent.spawned) == 1

    assert join_result.outcome == "success"
    assert join_result.metadata["reason"] == "joined"
    assert join_result.text == first_result.text  # 同一 run 的结果，带同一标签
    assert f'session_id="{sub_id}"' in join_result.text

    resume_events = [e for e in agent.session.events if e.get("type") == "sub_agent/resume"]
    assert len(resume_events) == 1
    assert resume_events[0]["mode"] == "join"
    assert get_job(sub_id) is None


def test_sub_session_hidden_from_user_session_list(session_env):
    from agents.core.session import list_sessions, is_derived_session_meta

    sub = Session(session_id="sub-x", parent_session="p", origin="sub_agent", agent_type="explore")
    sub.append("session/meta", {"origin": "sub_agent", "parent_session": "p", "agent_type": "explore"})
    sub.append("turn/end", {})
    main = Session(session_id="main-1")
    main.append("user_message", {"content": "hi"})
    main.append("turn/end", {})

    metas = {m["id"]: m for m in list_sessions()}
    assert "sub-x" in metas and "main-1" in metas  # 落盘可观测
    assert is_derived_session_meta(metas["sub-x"])  # 但不进用户列表
    assert not is_derived_session_meta(metas["main-1"])
    assert metas["sub-x"]["parent_session"] == "p"
    assert metas["sub-x"]["agent_type"] == "explore"


def test_spawn_passes_compression_arm_and_window(tmp_path, monkeypatch):
    """BC-20：子代理与父同压缩实验臂与窗口（LOCA 消融不再被子代理逃逸）。"""
    from agents.agent import Agent
    from agents.core.subagent_runner import spawn_sub_agent

    monkeypatch.setenv("MYCODE_SESSION_DIR", str(tmp_path / "sessions"))
    parent = Agent(
        model="unknown-model", api_base="https://example.com/v1", api_key="sk-test",
        compression_arm="truncate", context_window=123456,
    )
    child = spawn_sub_agent(parent, system_prompt="sp", tools=[], model_ref="", label="general",
                            agent_cls=Agent)
    assert child.compression_arm == "truncate"
    assert child.context_window == 123456
    assert child.is_sub_agent


def test_agent_tool_schema_has_session_id_and_no_plan_type():
    """D7：registry 静态 schema 与内置类型一致（plan 移除、reviewer 入列），session_id 续跑参数存在。"""
    from agents.tools.registry import tool_definitions

    agent_tool = next(t for t in tool_definitions if t["name"] == "agent")
    props = agent_tool["input_schema"]["properties"]
    assert "session_id" in props
    enum = props["type"]["enum"]
    assert "plan" not in enum
    assert {"explore", "reviewer", "general"} == set(enum)


def test_agent_tool_enum_refreshed_per_request(monkeypatch):
    """D7：type enum 每次请求现场重解析，自定义代理可被模型选择；原静态 schema 不被污染。"""
    from agents.core.model_caller import _to_openai_tools
    import agents.core.subagent as subagent_mod

    monkeypatch.setattr(subagent_mod, "get_available_agent_types", lambda: [
        {"name": "explore", "description": ""},
        {"name": "reviewer", "description": ""},
        {"name": "general", "description": ""},
        {"name": "my-custom", "description": ""},
    ])
    tools = [{
        "name": "agent",
        "description": "d",
        "input_schema": {
            "type": "object",
            "properties": {"type": {"type": "string", "enum": ["explore", "reviewer", "general"]}},
            "required": ["description", "prompt"],
        },
    }]
    out = _to_openai_tools(tools)
    enum = out[0]["function"]["parameters"]["properties"]["type"]["enum"]
    assert enum == ["explore", "reviewer", "general", "my-custom"]
    # deepcopy：原 tool_definitions 静态 schema 不被写脏
    assert tools[0]["input_schema"]["properties"]["type"]["enum"] == ["explore", "reviewer", "general"]
