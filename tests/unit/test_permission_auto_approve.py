"""审批不再超时 + 每会话自动审批开关（热生效）+ 三条退出路径都发 permission/resolved。

钉住的性质（编号对应简报「测试」一节）：
1. 开关已开 → gate.confirm() 返回 True 且**一个事件都不落**（裁定 2）。
2. 开关关 → 追加 permission/request；等待期间把开关打开 → 返回 True 且追加
   permission/resolved(outcome="auto")。这条同时钉住「热生效」与「裁定 2 的边界」：
   已落盘的请求必须补 resolved，否则历史恢复会把它复活成幽灵审批对话框。
3. 没有超时：不应答、不 abort 时，循环在远超旧 3000 轮（=300 秒）之后仍在等待。
4. abort 真能把无界循环放出来（唯一非人工出口）→ False + resolved(aborted)。
5. 人工应答追加 resolved（allowed / denied 各一条），last_choice/last_feedback 回填。
6. plan 审批自动放行落到 execute 路由（last_choice 空串 → `or "execute"`）。
7. 路由：GET 对非活跃会话返回 {"enabled": false}（不是 404）；PUT 翻转后 GET 读到新值。
8. request_permission 去掉 timeout 形参后照常工作（grep 全仓无调用方传过 timeout）。
9. 热生效全链路：_confirm_dangerous 等待期间，HTTP PUT /auto-approve 立刻放行。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from agents.agent import Agent
from agents.core.permission_gate import PermissionGate

_server_dir = str(Path(__file__).parent.parent.parent / "frontend" / "server")
if _server_dir not in sys.path:
    sys.path.insert(0, _server_dir)


class FakeSession:
    """只收集事件的会话替身（append 形状与 Session.append 一致）。"""

    def __init__(self) -> None:
        self.id = "s-fake"
        self.events: list[dict] = []

    def append(self, type_: str, data: dict | None = None) -> None:
        self.events.append({"type": type_, **(data or {})})

    def of_type(self, type_: str) -> list[dict]:
        return [e for e in self.events if e["type"] == type_]


def _make_agent() -> Agent:
    agent = Agent(model="deepseek-chat", api_base="https://example.com/v1", api_key="sk-test")
    agent.session = FakeSession()
    return agent


async def _wait_for_request(sess: FakeSession, timeout: float = 5.0) -> dict:
    """等到 permission/request 落盘并返回它（真实 sleep，测试绝不挂 300 秒）。"""
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while not sess.of_type("permission/request"):
        if loop.time() > deadline:
            raise AssertionError("permission/request 未落盘")
        await asyncio.sleep(0.02)
    return sess.of_type("permission/request")[-1]


# ── 1. 开关已开：直接放行、零事件（裁定 2）────────────────────────────────


async def test_auto_on_confirm_returns_true_without_any_event():
    sess = FakeSession()
    gate = PermissionGate(session=sess, auto_approve_fn=lambda: True)
    assert await gate.confirm("rm -rf /") is True
    assert sess.events == []  # 连 permission/request 都不许有
    assert gate.last_choice == "" and gate.last_feedback == ""


# ── 2. 等待中途翻转开关：放行 + 补 resolved(auto)（裁定 2 的边界）──────────


async def test_flip_during_wait_releases_and_appends_resolved_auto():
    sess = FakeSession()
    flag = {"on": False}
    gate = PermissionGate(session=sess, auto_approve_fn=lambda: flag["on"])
    task = asyncio.create_task(gate.confirm("dangerous"))
    req = await _wait_for_request(sess)  # 请求已落盘
    flag["on"] = True
    assert await asyncio.wait_for(task, 2) is True
    resolved = sess.of_type("permission/resolved")
    assert [r["outcome"] for r in resolved] == ["auto"]
    assert resolved[0]["rpc_id"] == req["rpc_id"]


# ── 3. 没有超时：循环无次数上界 ───────────────────────────────────────────


async def test_wait_loop_has_no_3000_round_cap(monkeypatch):
    sess = FakeSession()
    gate = PermissionGate(session=sess)  # 无 abort、无应答、无 auto —— 只能一直等

    class _Sentinel(Exception):
        pass

    iters = 0

    async def fake_sleep(_delay):
        nonlocal iters
        iters += 1
        if iters > 3500:  # 远超旧实现的 3000 轮（=300 秒）上界
            raise _Sentinel

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    with pytest.raises(_Sentinel):
        await gate.confirm("cmd")
    assert iters > 3000
    # 把实现改回 range(3000)+return False：confirm 会安静地返回 False，
    # pytest.raises(_Sentinel) 立刻红——旧缺陷不可能溜回来。


# ── 4. abort 是无界循环的非人工出口 ──────────────────────────────────────


async def test_abort_releases_wait_and_appends_resolved_aborted():
    sess = FakeSession()
    flag = {"abort": False}
    gate = PermissionGate(session=sess, abort_fn=lambda: flag["abort"])
    task = asyncio.create_task(gate.confirm("cmd"))
    req = await _wait_for_request(sess)
    flag["abort"] = True
    assert await asyncio.wait_for(task, 2) is False
    resolved = sess.of_type("permission/resolved")
    assert [r["outcome"] for r in resolved] == ["aborted"]
    assert resolved[0]["rpc_id"] == req["rpc_id"]


# ── 5. 人工应答：resolved(allowed/denied) + last_* 回填 ──────────────────


@pytest.mark.parametrize("allowed,outcome", [(True, "allowed"), (False, "denied")])
async def test_human_answer_appends_resolved(allowed, outcome):
    sess = FakeSession()
    gate = PermissionGate(session=sess)
    task = asyncio.create_task(gate.confirm("cmd"))
    req = await _wait_for_request(sess)
    gate.set_response(req["rpc_id"], allowed, feedback="because", choice="execute")
    assert await asyncio.wait_for(task, 2) is allowed
    resolved = sess.of_type("permission/resolved")
    assert [r["outcome"] for r in resolved] == [outcome]
    assert resolved[0]["rpc_id"] == req["rpc_id"]
    assert gate.last_feedback == "because"
    assert gate.last_choice == "execute"


# ── 6. plan 审批自动放行 → execute 路由 ──────────────────────────────────


def test_plan_auto_approval_route_moved_to_the_turn_end_hook():
    """`_request_approval` 随 exit_plan_mode 一起删了（spec §D3：审批不再阻塞在工具
    内部，改成 turn 收尾校验 plan.md 后落 permission/request）。

    它原来钉的性质——「自动放行 = execute 路由，且一个事件都不落」——搬到了 turn 收尾
    钩子，由 tests/unit/test_plan_mode_handoff.py 的
    test_auto_approve_on_at_turn_end_finalizes_without_request 钉住（那里断言
    finalize 收到的 choice 恰好是 "execute"、且 permission/request 为零条）。
    这里只钉删除面：符号真的不在了，别留一个没人调用的僵尸。
    """
    import agents.plan.plan_tool_executor as pte

    assert not hasattr(pte, "_request_approval")
    assert not hasattr(pte, "_exit_plan_mode")
    assert not hasattr(pte, "_finalize_plan_exit")


# ── 8. request_permission：无 timeout 形参，三条出口照常 ─────────────────


async def test_request_permission_auto_on_returns_true_without_events():
    agent = _make_agent()
    agent.set_auto_approve(True)
    assert await agent.request_permission("req-1", "cmd", "shell") is True
    assert agent.session.events == []


@pytest.mark.parametrize("allowed,outcome", [(True, "allowed"), (False, "denied")])
async def test_request_permission_human_answer(allowed, outcome):
    agent = _make_agent()
    task = asyncio.create_task(agent.request_permission("req-1", "cmd", "shell"))
    req = await _wait_for_request(agent.session)
    assert agent.respond_permission(req["rpc_id"], allowed) is True
    assert await asyncio.wait_for(task, 2) is allowed
    resolved = agent.session.of_type("permission/resolved")
    assert [r["outcome"] for r in resolved] == [outcome]
    assert resolved[0]["rpc_id"] == req["rpc_id"]
    assert not agent._permission_waiters  # waiter 已清理


async def test_request_permission_flip_mid_wait_appends_auto():
    agent = _make_agent()
    task = asyncio.create_task(agent.request_permission("req-1", "cmd", "shell"))
    req = await _wait_for_request(agent.session)
    agent.set_auto_approve(True)
    assert await asyncio.wait_for(task, 2) is True
    resolved = agent.session.of_type("permission/resolved")
    assert [r["outcome"] for r in resolved] == ["auto"]
    assert resolved[0]["rpc_id"] == req["rpc_id"]
    assert not agent._permission_waiters


async def test_request_permission_abort_appends_aborted():
    agent = _make_agent()
    task = asyncio.create_task(agent.request_permission("req-1", "cmd", "shell"))
    await _wait_for_request(agent.session)
    agent.abort()  # 绑定的 _abort_requested 翻真
    assert await asyncio.wait_for(task, 2) is False
    assert [r["outcome"] for r in agent.session.of_type("permission/resolved")] == ["aborted"]
    assert not agent._permission_waiters


# ── 9. 热生效链路：等待期间 set_auto_approve 直接放行门路径 ──────────────


async def test_confirm_dangerous_hot_released_by_set_auto_approve():
    agent = _make_agent()
    task = asyncio.create_task(agent._confirm_dangerous("rm -rf /", tool_name="shell"))
    await _wait_for_request(agent.session)
    agent.set_auto_approve(True)  # PUT 端点对同一个活 agent 做的就是这一下
    assert await asyncio.wait_for(task, 2) is True
    assert [r["outcome"] for r in agent.session.of_type("permission/resolved")] == ["auto"]


# ── 7. 路由端点 ──────────────────────────────────────────────────────────


def test_get_auto_approve_inactive_session_returns_false_not_404(monkeypatch):
    from routers import sessions as sessions_router
    monkeypatch.setattr(sessions_router, "_get_live_agent", lambda sid: None)
    from main import app

    r = TestClient(app).get("/api/sessions/nope/auto-approve")
    assert r.status_code == 200
    assert r.json() == {"enabled": False}


def test_put_auto_approve_404_when_agent_unavailable(monkeypatch):
    from routers import sessions as sessions_router

    async def fake_ensure(sid):
        return None

    monkeypatch.setattr(sessions_router, "_ensure_agent", fake_ensure)
    from main import app

    r = TestClient(app).put("/api/sessions/nope/auto-approve", json={"enabled": True})
    assert r.status_code == 404


async def test_put_auto_approve_hot_releases_waiting_confirm(monkeypatch):
    """PUT 翻转后：GET 读到新值，且**正在等待**的审批立刻放行（热生效全链路）。"""
    from routers import sessions as sessions_router

    agent = _make_agent()
    monkeypatch.setattr(sessions_router, "_get_live_agent", lambda sid: agent)

    async def fake_ensure(sid):
        return agent

    monkeypatch.setattr(sessions_router, "_ensure_agent", fake_ensure)
    from main import app

    client = TestClient(app)
    task = asyncio.create_task(agent._confirm_dangerous("rm -rf /", tool_name="shell"))
    await _wait_for_request(agent.session)

    r = client.put("/api/sessions/s-fake/auto-approve", json={"enabled": True})
    assert r.status_code == 200
    assert r.json() == {"success": True, "enabled": True}

    assert await asyncio.wait_for(task, 2) is True
    assert [e["outcome"] for e in agent.session.of_type("permission/resolved")] == ["auto"]

    r = client.get("/api/sessions/s-fake/auto-approve")
    assert r.status_code == 200
    assert r.json() == {"enabled": True}

    r = client.put("/api/sessions/s-fake/auto-approve", json={"enabled": False})
    assert r.json() == {"success": True, "enabled": False}
    assert client.get("/api/sessions/s-fake/auto-approve").json() == {"enabled": False}
