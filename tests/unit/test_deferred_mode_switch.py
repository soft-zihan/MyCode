"""plan → 其它模式的切换延迟到 step 边界生效（spec §D6 / 计划 Task C1）。

改动的形状：`Agent.set_permission_mode` 在「当前是 plan、目标不是、且回合正在跑」时
只把目标记进 `_pending_permission_mode`，由 `agent_loop.run()` 循环顶部（两次模型调用
之间那道缝，与 steering 的唯一注入点同一条缝）调 `apply_pending_permission_mode()`
落地；PUT /permission-mode 的响应体多一个 `deferred`，前端据此提示且**不**乐观更新。

钉住的性质，逐条都能回答「把实现改坏会不会红」：

1. 回合在跑时 plan → 其它**只记值**：permission_mode 不变、mode_changed 一个都不发
   （那一刻模式还没变，广播它就是撒谎）。改回立即生效 → 红。
2. 回合没在跑时**立刻**生效。把延迟扩大到所有切换 → 红（用户会在空转的会话上
   永远等不到生效，提示语还指向一个不存在的工具批次）。
3. 非 plan 模式的普通切换不被波及。把条件写成「无条件延迟」→ 红。
4. 待生效期间再切 = 覆盖（以最后一次为准）；切回 plan = 作废。改成排队 → 红。
5. 落地时：切到目标、只广播一次、待生效值清空、草稿目录清掉，而
   `_pending_plan_approval` **原样保留**（审批与模式是两件事）。
6. 已经不在 plan（批准/toggle 直接写 permission_mode、绕过 set_permission_mode）
   → apply 只清值不落地。去掉那道守卫 → 红（陈旧待生效值会盖掉批准刚切好的模式）。
7. 落地点就是 step 边界：第一次模型调用仍看到 plan，第二次看到新模式。
   把 apply 挪到模型调用之后、或从循环顶部删掉 → 红。
8. abort（循环顶部）/ error / cancel（turn_runner 两个分支）**清掉**待生效值、不落地。
9. PUT 响应带 deferred，且延迟时 permission_mode 报**真实**值而不是请求值。
"""
from __future__ import annotations

import asyncio
import contextlib
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from agents.agent import Agent
from agents.agent_loop import AgentLoop
from agents.core.turn_runner import TurnRunner
from agents.core.workspace import reset_workspace, set_workspace

_server_dir = str(Path(__file__).parent.parent.parent / "frontend" / "server")
if _server_dir not in sys.path:
    sys.path.insert(0, _server_dir)


@pytest.fixture(autouse=True)
def ws(tmp_path):
    """工作区作用域：进 plan 模式会建草稿目录，落地/退出会清它，都别写进仓库。"""
    token = set_workspace(tmp_path)
    yield tmp_path
    reset_workspace(token)


def _make_agent(**kwargs) -> Agent:
    defaults = dict(model="deepseek-chat", api_base="https://example.com/v1", api_key="sk-test")
    defaults.update(kwargs)
    return Agent(**defaults)


@contextlib.asynccontextmanager
async def _in_flight(agent: Agent):
    """让 `agent.is_processing` 为真 = 「回合正在跑」。

    `_current_task` 就是 turn_runner._run_turn_task 挂上去的那个任务，is_processing
    读的正是它。用一个永不完成的 Event().wait()（不是 sleep：不挂计时器），
    退出时 cancel + await，免得留下 pending task 警告。
    """
    task = asyncio.create_task(asyncio.Event().wait())
    agent._current_task = task
    try:
        yield agent
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        agent._current_task = None


def _mode_changes(agent) -> list[str]:
    return [e["mode"] for e in agent.session.events
            if e.get("type") == "permission/mode_changed"]


# ── 1/2/3. 什么时候延迟、什么时候立刻 ────────────────────────────────────────


async def test_plan_to_other_is_deferred_while_a_turn_is_in_flight():
    agent = _make_agent(permission_mode="plan")
    async with _in_flight(agent):
        agent.set_permission_mode("acceptEdits")
        assert agent.permission_mode == "plan"                   # 没在回合中间变脸
        assert agent._pending_permission_mode == "acceptEdits"    # 只记值
        assert _mode_changes(agent) == []                         # 且不广播（撒谎）


async def test_idle_plan_to_other_switch_is_immediate():
    """空转的会话上没有「回合中间」可保护，所以立刻生效。

    这条钉住的是延迟的**边界**：无条件延迟会让用户在没有任何工具批次可等的会话上
    永远看不到模式变化（选择器停在 plan、提示语指向不存在的批次）。
    """
    agent = _make_agent(permission_mode="plan")
    agent.set_permission_mode("acceptEdits")
    assert agent.permission_mode == "acceptEdits"
    assert agent._pending_permission_mode is None
    assert _mode_changes(agent) == ["acceptEdits"]


async def test_non_plan_switch_is_never_deferred():
    agent = _make_agent(permission_mode="default")
    async with _in_flight(agent):
        agent.set_permission_mode("acceptEdits")
        assert agent.permission_mode == "acceptEdits"
        assert agent._pending_permission_mode is None
        assert _mode_changes(agent) == ["acceptEdits"]


# ── 4. 最后一次为准 ─────────────────────────────────────────────────────────


async def test_last_write_wins_and_switching_back_cancels():
    agent = _make_agent(permission_mode="plan")
    async with _in_flight(agent):
        agent.set_permission_mode("acceptEdits")
        agent.set_permission_mode("default")
        assert agent._pending_permission_mode == "default"    # 覆盖，不排队
        agent.set_permission_mode("plan")                     # 边界到来之前又切回来
        assert agent._pending_permission_mode is None         # 待生效值作废
        assert agent.permission_mode == "plan"
        assert _mode_changes(agent) == []


# ── 5/6. 落地 ──────────────────────────────────────────────────────────────


async def test_apply_lands_once_and_keeps_the_pending_plan_approval():
    agent = _make_agent(permission_mode="plan")
    # 审批与模式是两件事：延迟切换不能把一条仍然活着的非阻塞审批弄丢
    # （spec 计划 Task C1 的 ⚠️ 那条）。
    agent.set_pending_plan_approval({"rpc_id": "abc123", "draft": object()})

    async with _in_flight(agent):
        agent.set_permission_mode("acceptEdits")
    agent.apply_pending_permission_mode()

    assert agent.permission_mode == "acceptEdits"
    assert agent._pending_permission_mode is None
    assert _mode_changes(agent) == ["acceptEdits"]              # 恰好广播一次
    assert agent._plan_mode_manager.plan_dir is None            # 草稿目录已清
    assert agent._plan_mode_manager.pre_plan_mode is None
    assert agent._pending_plan_approval is not None
    assert agent._pending_plan_approval["rpc_id"] == "abc123"

    agent.apply_pending_permission_mode()                       # 幂等：不重复广播
    assert _mode_changes(agent) == ["acceptEdits"]


async def test_apply_is_a_noop_after_plan_was_left_by_another_route():
    """finalize_approved_plan / toggle_plan_mode / CLI 都**直接**写 permission_mode，
    绕过 set_permission_mode，于是待生效值可能活到「已经不是 plan」的时刻。
    没有这道守卫，批准刚切好的 acceptEdits 会被一个陈旧的待生效值盖回 default。"""
    agent = _make_agent(permission_mode="plan")
    async with _in_flight(agent):
        agent.set_permission_mode("default")
    assert agent._pending_permission_mode == "default"

    agent.permission_mode = "acceptEdits"          # 批准落地就是这么写的
    agent.apply_pending_permission_mode()

    assert agent.permission_mode == "acceptEdits"   # 没被盖掉
    assert agent._pending_permission_mode is None    # 但值清掉了
    assert _mode_changes(agent) == []


async def test_entering_plan_again_discards_a_stale_pending_switch():
    """同一个洞的另一半：从**别的入口**（enter_plan_mode 工具 / CLI）再进 plan 时，
    陈旧的待生效值必须作废，否则下一个 step 边界会把用户刚进入的 plan 模式踢出去。"""
    agent = _make_agent(permission_mode="plan")
    async with _in_flight(agent):
        agent.set_permission_mode("default")
    agent.permission_mode = "acceptEdits"          # 批准落地，待生效值成了陈旧值

    agent._enter_plan_mode_internal()              # 模型又调了 enter_plan_mode

    assert agent.permission_mode == "plan"
    assert agent._pending_permission_mode is None
    agent.apply_pending_permission_mode()
    assert agent.permission_mode == "plan"         # 没被踢出去


# ── 7. 落地点 = step 边界（靶心）───────────────────────────────────────────


async def test_deferred_switch_lands_at_the_step_boundary(monkeypatch):
    """真跑 agent_loop.run()：只桩模型调用与工具批次，循环体本身是真的。

    断言的是**每次模型调用看到的模式序列** == ["plan", "acceptEdits"]：
    第一次调用时切换还没落地（不在回合中间变脸），第二次调用前落地了
    （落在两次模型调用之间那道缝上）。把 apply 挪到 `_run_step` 之后、
    挪到回合收尾、或者删掉，这条都会红。
    """
    agent = _make_agent(permission_mode="plan")
    loop = AgentLoop(agent)
    modes_seen: list[str] = []
    calls = {"n": 0}

    async def _fake_run_step():
        calls["n"] += 1
        modes_seen.append(agent.permission_mode)
        if calls["n"] == 1:
            # 第一个工具批次正在跑的时候用户切了模式（is_processing 为真 → 延迟）
            agent.set_permission_mode("acceptEdits")
            return [{"id": "c1", "type": "function",
                     "function": {"name": "read_file", "arguments": "{}"}}], 1
        return None, 0

    async def _fake_handle_tool_calls(tool_calls, assistant_seq=None):
        return False

    async def _noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr(loop, "_prepare_turn", _noop)
    monkeypatch.setattr(loop, "_consume_wiki_prefetch", _noop)
    monkeypatch.setattr(loop, "_finalize_text_response", _noop)
    monkeypatch.setattr(loop, "_run_step", _fake_run_step)
    monkeypatch.setattr(loop, "_handle_tool_calls", _fake_handle_tool_calls)

    async with _in_flight(agent):
        # 30s 上限而不是 3s：全套压上来时事件循环会慢，紧上限会产假失败。
        await asyncio.wait_for(loop.run(None), 30)

    assert modes_seen == ["plan", "acceptEdits"]
    assert calls["n"] == 2
    assert _mode_changes(agent) == ["acceptEdits"]
    assert agent._pending_permission_mode is None


# ── 8. 异常路径清掉、不落地 ────────────────────────────────────────────────


async def test_abort_at_the_loop_top_discards_the_pending_switch(monkeypatch):
    agent = _make_agent(permission_mode="plan")
    async with _in_flight(agent):
        agent.set_permission_mode("acceptEdits")
        assert agent._pending_permission_mode == "acceptEdits"

    agent._aborted = True                     # abort() 之后循环看到的状态
    loop = AgentLoop(agent)

    async def _noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr(loop, "_prepare_turn", _noop)
    await asyncio.wait_for(loop.run(None), 30)

    assert agent.permission_mode == "plan"          # 没落地
    assert agent._pending_permission_mode is None    # 清掉了
    assert _mode_changes(agent) == []


async def test_turn_error_discards_the_pending_switch():
    """turn_runner._run_turn_task 的 except Exception 分支（spec §D6：error 路径）。"""
    agent = _make_agent(permission_mode="plan")
    agent._pending_permission_mode = "acceptEdits"   # 回合中途记下的待生效值

    async def _boom(user_message):
        raise RuntimeError("model exploded")

    agent._chat_openai = _boom
    span = SimpleNamespace(add_metadata=lambda **_kw: None, record_error=lambda _e: None)

    assert await TurnRunner(agent)._run_turn_task("hi", "hi", span) is None
    assert agent.permission_mode == "plan"
    assert agent._pending_permission_mode is None


async def test_turn_cancel_discards_the_pending_switch():
    """同一条纪律的另一半：真的 abort 走的是 CancelledError 分支。"""
    agent = _make_agent(permission_mode="plan")
    agent._pending_permission_mode = "acceptEdits"

    async def _cancelled(user_message):
        raise asyncio.CancelledError()

    agent._chat_openai = _cancelled
    span = SimpleNamespace(add_metadata=lambda **_kw: None, record_error=lambda _e: None)

    with pytest.raises(asyncio.CancelledError):
        await TurnRunner(agent)._run_turn_task("hi", "hi", span)
    assert agent.permission_mode == "plan"
    assert agent._pending_permission_mode is None


# ── 9. PUT 响应里的 deferred（spec §D6 刻意选的可观测信号）──────────────────


def test_put_permission_mode_reports_deferred(monkeypatch):
    from routers import sessions as sessions_router

    agent = _make_agent(permission_mode="plan")
    # is_processing 为真，但不真起任务：端点经 TestClient 跑在另一个线程/循环里。
    agent._current_task = SimpleNamespace(done=lambda: False)

    async def fake_ensure(_sid):
        return agent

    monkeypatch.setattr(sessions_router, "_ensure_agent", fake_ensure)
    from main import app

    client = TestClient(app)
    r = client.put("/api/sessions/s-x/permission-mode", json={"mode": "acceptEdits"})
    assert r.status_code == 200, r.text
    # permission_mode 报**真实**值：延迟时回请求值等于声称一个还没生效的模式。
    assert r.json() == {"success": True, "permission_mode": "plan", "deferred": True}

    # 反面对照：待生效值落地之后再切（此时已不在 plan）→ 立即生效、deferred false。
    # 没有这条，「deferred 恒为 true」也能让上面那半通过。
    agent.apply_pending_permission_mode()
    r2 = client.put("/api/sessions/s-x/permission-mode", json={"mode": "default"})
    assert r2.json() == {"success": True, "permission_mode": "default", "deferred": False}
