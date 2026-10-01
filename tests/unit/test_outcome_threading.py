"""outcome 的三段穿线 —— 验收闸门唯一的事实来源，此前三行代码没有任何回归网。

闸门（agents/tools/task_gate.py）不接受 agent 自报，只能事后从落盘的
tool_result_msg 读事实。那条事实靠三行代码送到事件日志里：

    agents/agent_loop.py  并发批   a.append_tool_message(id, res, fn, outcome)
    agents/agent_loop.py  顺序批   a.append_tool_message(id, res, fn, outcome)
    agents/agent.py       透传     self._context_manager.append_tool_message(
                                       tool_call_id, content, tool_name, outcome)

删掉其中任何一行，全套测试仍然绿 —— ContextManager 那一层已被
tests/unit/test_tool_outcome_events.py 覆盖，缺口恰恰在于没人从 **Agent 这一侧**
驱动过它。后果是线上每条 tool_result_msg 都落 outcome=""，闸门永久失明，
而"标 completed 却没有验证证据"的警告一条都发不出来（静默失效，没有报错）。

所以本文件从 Agent.append_tool_message 与 AgentLoop 两条真实执行路径驱动，
断言的是**落盘后的事件**，不是中间层的参数。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from agents.agent import Agent
from agents.agent_loop import AgentLoop
from agents.core.context import ContextManager
from agents.core.session import Session
from agents.core.workspace import reset_workspace, set_workspace
from agents.tools.result import ToolExecutionResult


@pytest.fixture(autouse=True)
def session_env(tmp_path, monkeypatch):
    """重定向 MYCODE_SESSION_DIR 并复位全局 backend / 投影缓存。

    与 tests/unit/test_tool_outcome_events.py 同一约定：两个 backend 都在**每次
    访问**时解析 session_dir()，复位 _backend 让 MYCODE_SESSION_BACKEND 在本模块
    内被重新读取——jsonl 与 sqlite 两条腿跑的是同一份断言。
    """
    monkeypatch.setenv("MYCODE_SESSION_DIR", str(tmp_path / "sessions"))
    import agents.core.session as sess
    import agents.core.session_projection_cache as spc

    monkeypatch.setattr(sess, "_backend", None)
    monkeypatch.setattr(spc, "_projection_cache", None)
    yield tmp_path
    monkeypatch.setattr(sess, "_backend", None)
    monkeypatch.setattr(spc, "_projection_cache", None)


@pytest.fixture
def ws(tmp_path):
    token = set_workspace(tmp_path)
    yield tmp_path
    reset_workspace(token)


def _make_agent(**kwargs) -> Agent:
    """真实 Agent（假端点，不发请求）——刻意不用替身。

    用替身就会绕过 agents/agent.py 里那行透传，而那正是本文件要钉的三行之一。
    """
    defaults = dict(model="stub", api_base="https://example.com/v1", api_key="sk-test")
    defaults.update(kwargs)
    return Agent(**defaults)


def _tool_events(session) -> list[dict]:
    return [e for e in session.events if e.get("type") == "tool_result_msg"]


# ---- 第一段：Agent.append_tool_message → ContextManager → 事件 ----


def test_agent_append_tool_message_persists_outcome(session_env):
    """agents/agent.py 的透传：Agent 这一侧必须把 outcome 送到事件日志。

    只测 ContextManager 测不到这行——它的签名与 ContextManager 的同名方法
    长得一样，漏传第 4 个参数时 ContextManager 的默认值 "" 静默接管。
    """
    agent = _make_agent()
    agent.append_tool_message("c1", "输出", "run_shell", "success")
    ev = _tool_events(agent.session)[0]
    assert ev["outcome"] == "success"
    assert ev["tool_name"] == "run_shell"


def test_agent_append_tool_message_threads_error_outcome_verbatim(session_env):
    """outcome 逐字落盘，不在 Agent 这层被归一化（闸门按 == "success" 精确比较）。"""
    agent = _make_agent()
    agent.append_tool_message("c1", "boom", "run_shell", "error")
    assert _tool_events(agent.session)[0]["outcome"] == "error"


def test_agent_append_tool_message_defaults_to_empty_for_cancel_paths(session_env):
    """取消/中止/权限拒绝路径不传这两个参数 → 落空串，不是缺键也不是 None。

    空串是一个** overloaded ** 值：它同时覆盖取消、中止与权限拒绝。闸门只需要
    `"" != "success"`，绝不从中反推"被拒绝"（那会断言兄弟事件没有断言的事）。
    """
    agent = _make_agent()
    agent.append_tool_message("c1", "Action cancelled: user abort.")
    ev = _tool_events(agent.session)[0]
    assert ev["outcome"] == ""
    assert ev["tool_name"] == ""


# ---- 第二、三段：AgentLoop 并发批 / 顺序批 → Agent.append_tool_message ----


def _make_loop_stub_agent(outcome: str = "success"):
    """AgentLoop 的最小 agent 替身，但 append_tool_message 用**真实的** Agent 方法。

    `Agent.append_tool_message.__get__(stub)` 把未绑定的函数装到替身上，于是
    执行链是 agent_loop → 真 Agent.append_tool_message → 真 ContextManager →
    真 Session：三段穿线一次钉住，任何一段漏传 outcome 都会让断言的事件变成 ""。
    代价是替身要提供该方法触达的两个属性（_context_manager / _tool_result_chars）。
    """
    session = Session(session_id="thread-stub", origin="sub_agent")
    stub = SimpleNamespace(
        session=session,
        session_id=session.id,
        _current_turn=1,
        _current_step=0,
        _tool_call_count=0,
        _tool_result_chars={},
        max_tool_calls=None,
        abort_requested=lambda: False,
        publish_tool_result_event=lambda *a, **k: None,
        record_tool_outcome=lambda *a, **k: None,
        persist_large_result=lambda fn, raw: raw,
        looks_like_tool_failure=lambda fn, text, res: False,
        context_cleared=False,
        clear_context_flag=lambda: None,
        _repeat_guard=SimpleNamespace(check=lambda fn, inp: None),
    )
    stub._context_manager = ContextManager(agent_ref=stub)
    stub.append_tool_message = Agent.append_tool_message.__get__(stub)

    async def execute_tool_call(name, inp, assistant_seq=None):
        return ToolExecutionResult(text="ok", status="ok", outcome=outcome)

    stub.execute_tool_call = execute_tool_call
    return stub


def _make_loop(stub) -> AgentLoop:
    loop = AgentLoop(stub)
    loop._auto_mark_bad_case = lambda *a, **k: None
    return loop


async def test_concurrent_batch_threads_outcome_into_the_persisted_event(ws):
    """agent_loop 并发批那条 append_tool_message 必须带 outcome。

    read_file ∈ CONCURRENCY_SAFE_TOOLS → 走并发批。
    """
    stub = _make_loop_stub_agent("success")
    items = [
        {"tc": {"id": "c1"}, "fn": "read_file", "inp": {"file_path": "x"}, "allowed": True},
    ]
    await _make_loop(stub)._execute_tool_batches(items, 77)
    ev = _tool_events(stub.session)[0]
    assert ev["tool_name"] == "read_file"
    assert ev["outcome"] == "success"


async def test_sequential_batch_threads_outcome_into_the_persisted_event(ws):
    """agent_loop 顺序批那条 append_tool_message 必须带 outcome。

    task_list 不在 CONCURRENCY_SAFE_TOOLS → 走顺序批。闸门的证据工具是
    run_shell，同样是顺序批，所以这条路径漏传会让闸门对**所有**任务失明。
    """
    stub = _make_loop_stub_agent("success")
    items = [
        {"tc": {"id": "c1"}, "fn": "task_list",
         "inp": {"operation": "list"}, "allowed": True},
    ]
    await _make_loop(stub)._execute_tool_batches(items, 77)
    ev = _tool_events(stub.session)[0]
    assert ev["tool_name"] == "task_list"
    assert ev["outcome"] == "success"


async def test_failed_tool_threads_error_outcome_through_the_loop(ws):
    """非 success 的 outcome 也要逐字穿过去——闸门靠它把"跑了但失败"排除在外。"""
    stub = _make_loop_stub_agent("error")
    items = [
        {"tc": {"id": "c1"}, "fn": "run_shell", "inp": {"command": "false"},
         "allowed": True},
    ]
    await _make_loop(stub)._execute_tool_batches(items, 77)
    assert _tool_events(stub.session)[0]["outcome"] == "error"


async def test_denied_tool_persists_empty_outcome_not_an_invented_one(ws):
    """权限拒绝路径落空串：那里没有 ToolExecutionResult，不就地编一个词表值。

    这条与"闸门不得把空串读成 denied"是一对：事件只断言"没有 outcome"，
    闸门只断言"没有成功证据"，两边都不多说。
    """
    stub = _make_loop_stub_agent("success")
    items = [
        {"tc": {"id": "c1"}, "fn": "run_shell", "inp": {"command": "rm -rf /"},
         "allowed": False, "result": "Error: permission denied"},
    ]
    await _make_loop(stub)._execute_tool_batches(items, 77)
    ev = _tool_events(stub.session)[0]
    assert ev["outcome"] == ""
    assert ev["tool_name"] == "run_shell"
