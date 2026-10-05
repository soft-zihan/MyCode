"""后端重启后仍然活着的计划审批（spec §十.6 / 批次 C 的 C3）。

缺陷形状：`Agent._pending_plan_approval` 是内存态，重启就没了；而前端照旧能从历史
恢复出审批对话框（有 `permission/request{tool_name:"plan_approval"}`、没有配对的
`permission/resolved`）。用户点批准 → `take_pending_plan_approval` 返回 None →
掉进 `/api/events/respond` 末尾那条 `set_permission_response`（没有等待者）→
**什么都不发生、也不报错**。UI 显示成功、实际无效、零可观测。

修法是**从盘上重建**而不是加持久化：判据与前端那条恢复逻辑同一条（未配对的
plan_approval 请求 = 审批仍然活着），计划正文本来就还在 `{plan_dir}/plan.md` 上，
重新装载 + 重新校验，然后交回**同一个** `resolve_plan_approval`。

钉住的性质，逐条都能回答「把实现改坏会不会红」：

1. 未配对的请求 + 盘上合格的 plan.md → 重建出 pending（draft 是重新装载的正文），
   并把 agent 恢复回 plan 模式（UI 于是显示真实模式，而不是重启后那个假的默认值）。
   删掉 `_enter_plan_mode_internal()` 那一步、或改成自己 new 一个 draft 对象 → 红。
2. 重新校验不合格（用户手工把 plan.md 改坏了）→ 返回**明确错误字符串**，
   把校验器的可执行原文透传出去。改成返回 None（= 静默掉进权限门那条路）→ 红。
3. 已配对 resolved（批准过 / 拒绝过 / superseded / 自动放行）→ None。
   这是与 `take_pending_plan_approval`「取出即清空」同一种幂等：双击、前端重试
   第二次走到这里时不能再 finalize 一遍，否则同一批任务被物化进 task_list 两次。
4. 与计划审批无关的 rpc_id（危险命令确认）→ None 且**零副作用**，照旧走权限门。
5. /api/events/respond 端到端：重建成功交回的是**同一个** resolve_plan_approval
   （没有第二份 finalize）；重建失败回 `{"success": false, "message": ...}`，
   既不掉进 set_permission_response、也不假装 finalize，并且**不落 resolved**
   ——审批仍然活着，用户改好 plan.md 之后还能再批一次。
6. 走**真的** resolve_plan_approval 能落地：任务物化进 task_list、模式切到
   acceptEdits、resolved 落盘、新回合的起始消息是 spec §D8 那行当前状态声明。
   这条证明重建出的 draft 就是 finalize 能用的那一份，不是形状凑巧对得上。
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from agents.agent import Agent
from agents.core.session import Session
from agents.core.workspace import reset_workspace, set_workspace, workspace_scope
from agents.plan.plan_tool_executor import PLAN_APPROVAL_TOOL_NAME
from agents.tools.task_store import list_tasks

_server_dir = str(Path(__file__).parent.parent.parent / "frontend" / "server")
if _server_dir not in sys.path:
    sys.path.insert(0, _server_dir)

from routers.plan_approval import rebuild_pending_plan_approval, resolve_plan_approval  # noqa: E402
from routers import plan_approval as plan_approval_router  # noqa: E402
from routers import sessions as sessions_router  # noqa: E402

SID = "s-rebuild"
RPC = "rpc-restart-1"

GOOD_PLAN = """# 加一个 clamp 函数

## 背景
utils.py 缺区间限制。

## 方案
末尾追加 clamp。

## 任务清单

- [ ] 1. 实现 clamp
  - 验收: python -c "from utils import clamp"
  - 改动: utils.py

## 验收
上面那条命令通过。
"""

# 缺 H1（写成 ##）+ 缺 `## 任务清单` 精确标题：校验器必判不合格（硬规则 1/2）。
BAD_PLAN = """## 加一个 clamp 函数

## Tasks

- [ ] 1. 实现 clamp
"""


@pytest.fixture(autouse=True)
def ws(tmp_path):
    token = set_workspace(tmp_path)
    yield tmp_path
    reset_workspace(token)


def _restarted_agent(
    tmp_path,
    *,
    plan_content: str | None = GOOD_PLAN,
    resolved_outcome: str | None = None,
    tool_name: str = PLAN_APPROVAL_TOOL_NAME,
    rpc_id: str = RPC,
) -> Agent:
    """造一个「后端重启之后」的现场：事件与草稿都在盘上，内存态一个都不剩。

    刻意分两步——先用一个 Session 把事件写进后端（= 重启前那个进程干的），再
    `Session.load_from_events` + 新构造的 Agent（= session_manager.restore 干的事）。
    直接往活 agent 的内存 session 里塞事件是抓不到这个缺陷的：那样
    `_pending_plan_approval` 根本不会丢。
    """
    draft_dir = tmp_path / ".mycode" / "plans" / f"plan-{SID}"
    draft_dir.mkdir(parents=True, exist_ok=True)
    if plan_content is not None:
        (draft_dir / "plan.md").write_text(plan_content, encoding="utf-8")

    before = Session(session_id=SID, origin="test")
    before.append("permission/request", {
        "rpc_id": rpc_id,
        "request_id": rpc_id,
        "command": plan_content or "",
        "tool_name": tool_name,
        "message": "计划已写完并通过格式校验，等待批准。",
    })
    if resolved_outcome is not None:
        before.append("permission/resolved", {"rpc_id": rpc_id, "outcome": resolved_outcome})

    agent = Agent(model="deepseek-chat", api_base="https://example.com/v1",
                  api_key="sk-test", session_id=SID, workspace=tmp_path)
    agent.session = Session.load_from_events(SID)
    assert agent.session is not None
    # 现场核对：内存态确实丢了（否则本文件测的不是重启路径）
    assert agent._pending_plan_approval is None
    assert agent._plan_mode_manager.plan_dir is None
    assert agent.permission_mode != "plan"
    return agent


def _events(agent, type_: str) -> list[dict]:
    return [e for e in agent.session.events if e.get("type") == type_]


# ── 1. 重建 ────────────────────────────────────────────────────────────────


def test_rebuild_reloads_the_plan_from_disk(tmp_path):
    agent = _restarted_agent(tmp_path)

    pending = rebuild_pending_plan_approval(agent, RPC)

    assert isinstance(pending, dict)
    assert pending["rpc_id"] == RPC
    assert "clamp" in pending["draft"].full_plan        # 正文是从盘上重新装载的
    # 一条挂着的计划审批就意味着这个会话当时在 plan 模式：恢复回去，UI 才会显示
    # 真实模式（草稿目录也随之回来——generate_plan_dir 由 session_id 决定，同路径）
    assert agent.permission_mode == "plan"
    assert agent._plan_mode_manager.plan_dir is not None
    assert [e["mode"] for e in _events(agent, "permission/mode_changed")] == ["plan"]
    # 重建**不**落地任何审批动作：resolved 一条都没有（那是 resolve_plan_approval 的事）
    assert _events(agent, "permission/resolved") == []


def test_rebuild_reports_a_clear_error_when_the_plan_no_longer_validates(tmp_path):
    """用户在重启前后把 plan.md 手工改坏了 → 明确错误，**不是** None。

    None 的含义是「这不是一条活着的计划审批」（调用方照旧走权限门那条路），
    用它表示失败就会把缺陷原样复现：静默、无操作、零可观测。
    """
    agent = _restarted_agent(tmp_path, plan_content=BAD_PLAN)

    result = rebuild_pending_plan_approval(agent, RPC)

    assert isinstance(result, str)
    assert "校验" in result
    assert "任务清单" in result        # 校验器那句可执行的错误原文透传给用户


# ── 3/4. 什么时候返回 None ─────────────────────────────────────────────────


def test_rebuild_returns_none_once_the_request_is_resolved(tmp_path):
    """幂等：已决的审批不能被重建出来再 finalize 一遍（任务会物化两次）。"""
    agent = _restarted_agent(tmp_path, resolved_outcome="allowed")
    assert rebuild_pending_plan_approval(agent, RPC) is None


def test_rebuild_ignores_unrelated_permission_requests(tmp_path):
    """危险命令确认那条路必须原样保留：不是计划审批 → None 且**零副作用**。"""
    agent = _restarted_agent(tmp_path, tool_name="run_shell")

    assert rebuild_pending_plan_approval(agent, RPC) is None
    assert rebuild_pending_plan_approval(agent, "some-other-rpc") is None
    assert agent.permission_mode != "plan"                       # 没被莫名其妙切进 plan
    assert _events(agent, "permission/mode_changed") == []


# ── 5. /api/events/respond 端到端 ──────────────────────────────────────────


def _wire_endpoint(monkeypatch, agent):
    from routers import events as events_router

    # resolve_session_workspace 走「活跃会话取 agent.workspace」这一支
    monkeypatch.setattr(sessions_router, "_get_live_agent", lambda _sid: agent)

    async def fake_ensure(_sid):
        return agent

    monkeypatch.setattr(events_router, "_ensure_agent", fake_ensure)
    from main import app

    return TestClient(app)


def _respond(client, *, allowed=True, choice="execute", feedback=""):
    return client.post("/api/events/respond", json={
        "rpc_id": RPC, "allowed": allowed, "session_id": SID,
        "feedback": feedback, "choice": choice,
    })


def test_respond_after_restart_reuses_the_same_resolve_path(tmp_path, monkeypatch):
    """靶心：重建之后交回的是**同一个** resolve_plan_approval，没有第二份 finalize。"""
    agent = _restarted_agent(tmp_path)
    client = _wire_endpoint(monkeypatch, agent)

    seen: list[tuple] = []

    def fake_resolve(sm, ag, pending, **kwargs):
        seen.append((ag, pending, kwargs))
        return {"success": True, "outcome": "allowed"}

    monkeypatch.setattr(plan_approval_router, "resolve_plan_approval", fake_resolve)

    r = _respond(client)
    assert r.status_code == 200, r.text
    assert r.json() == {"success": True, "outcome": "allowed"}
    assert len(seen) == 1
    ag, pending, kwargs = seen[0]
    assert ag is agent
    assert pending["rpc_id"] == RPC
    assert "clamp" in pending["draft"].full_plan
    assert kwargs == {"allowed": True, "choice": "execute", "feedback": ""}


def test_respond_reports_the_failure_instead_of_a_silent_noop(tmp_path, monkeypatch):
    """缺陷的正面复现测试：重建失败时既不能静默、也不能掉进没人等待的权限门路径。"""
    agent = _restarted_agent(tmp_path, plan_content=BAD_PLAN)
    client = _wire_endpoint(monkeypatch, agent)

    gate_calls: list = []
    agent.set_permission_response = lambda *a, **k: gate_calls.append(a)
    resolve_calls: list = []
    monkeypatch.setattr(
        plan_approval_router, "resolve_plan_approval",
        lambda *a, **k: resolve_calls.append(a) or {"success": True},
    )

    body = _respond(client).json()

    assert body["success"] is False
    assert "校验" in body["message"]          # 用户看得到原因
    assert gate_calls == []                    # 没掉进那条静默路径
    assert resolve_calls == []                 # 也没假装 finalize
    # 审批仍然活着（请求没有配对的 resolved）：用户改好 plan.md 之后还能再批一次
    assert _events(agent, "permission/resolved") == []


# ── 6. 走真的 resolve_plan_approval ────────────────────────────────────────


def test_rebuilt_approval_finalizes_for_real(tmp_path, monkeypatch):
    """不做任何桩（除了起新回合那一步）：证明重建出的 draft 就是 finalize 能用的那份。"""
    agent = _restarted_agent(tmp_path)
    monkeypatch.setattr(sessions_router, "_get_live_agent", lambda _sid: agent)
    spawned: list[str] = []
    monkeypatch.setattr(
        plan_approval_router, "_spawn_chat_task",
        lambda sm, ag, sess, message: spawned.append(message),
    )

    pending = rebuild_pending_plan_approval(agent, RPC)
    assert isinstance(pending, dict)
    result = resolve_plan_approval(SimpleNamespace(), agent, pending, allowed=True, choice="execute")

    assert result == {"success": True, "outcome": "allowed"}
    assert agent.permission_mode == "acceptEdits"
    assert agent._plan_mode_manager.plan_dir is None       # 草稿目录已清
    assert len(spawned) == 1
    assert spawned[0].startswith("当前权限模式：acceptEdits")   # spec §D8 的当前状态行
    with workspace_scope(tmp_path):
        tasks = list_tasks(SID)
    assert len(tasks) == 1 and "clamp" in tasks[0].content    # 任务真的物化了
    # 请求已配对 resolved，否则前端的「从历史恢复待审批」会把它复活成幽灵对话框
    assert [e["outcome"] for e in _events(agent, "permission/resolved")] == ["allowed"]
