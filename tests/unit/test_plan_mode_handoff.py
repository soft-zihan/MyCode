"""plan 模式的 turn 收尾送审 + 非阻塞审批（spec §D1/§D2/§D3/§D9/§十.2/§十.3）。

钉住的性质，逐条都能回答「把实现改坏会不会红」：

1. 合格计划 → 落 permission/request{tool_name:"plan_approval"}，且**回合正常结束**
   （run() 在 30s 内返回，没有协程挂着等审批——非阻塞是 spec §D3 的全部要点，
   原 600s 工具超时缺陷正是阻塞式的产物）。
2. 不合格 → 错误清单作为 user_message 落盘、循环 continue 让模型自己修。
3. 自动修正**有界**：恰好 2 次，第 3 次结束回合并把错误落成用户可见的诊断。
4. permission_mode != "plan" → 钩子严格短路，连校验器都不读（spec §十.2：这条路径
   是**所有会话**共享的）。
5. 校验器抛异常 → 不送审、不阻塞回合，但**留可观测痕迹**（C1 缺陷类的教训：
   静默降级 + 上层成功信号是本项目最贵的故障形态）。
6. 自动审批开关已开 → 不落 permission/request，当场 finalize（spec §D9）。
7. 已有待审批时又产出一份新的合格计划 → 旧 rpc_id 落 resolved{outcome:"superseded"}
   （spec §十.3：漏了它，用户会批准一份已经不是当前内容的计划）。
8. `_plan_validation_attempts` 每回合归零，而 `_pending_plan_approval` 跨回合存活
   ——两者的生命周期刻意相反，钉在一起防止「顺手一起清」。
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from agents.agent_loop import AgentLoop
from agents.core.session import Session
from agents.core.steer_queue import MessageQueue
from agents.core.turn_runner import TurnRunner
from agents.plan.plan_mode import PlanModeManager

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

# 缺 H1（写成 ##）+ 缺 `## 任务清单` 精确标题：两条硬错误，校验器必判不合格。
BAD_PLAN = """## 加一个 clamp 函数

## Tasks

- [ ] 1. 实现 clamp
"""


class HandoffFakeAgent:
    """走到真实 turn 收尾钩子的最小 agent 替身。

    样板取自 tests/unit/test_human_wait_tool_timeout.py 的 PlanFakeAgent——那是本仓库
    「用替身走到真实审批路径」的现成例子。**校验器刻意用真的 PlanModeManager**：
    钩子消费的是它的返回形状契约（valid/errors/warnings/task_count），桩掉校验器
    等于把契约一起桩掉，那正是本项目吃过的假阴性形态。
    """

    def __init__(self, workspace: Path, *, permission_mode: str = "plan",
                 plan_content: str | None = GOOD_PLAN) -> None:
        self.session = Session(session_id="s-handoff", origin="test")
        self.session_id = self.session.id
        self.permission_mode = permission_mode
        self.is_sub_agent = False
        self.current_sub_agent_id = None

        # turn_runner._reset_turn_state 与 agent_loop 收尾路径读到的那一组状态
        self._current_turn = 0
        self._current_step = 0
        self._aborted = False
        self._abort_event = asyncio.Event()
        self._turn_number = 0
        self._turn_output_buffer: list[str] = []
        self._turn_thinking_buffer: list[str] = []
        self._turn_event_buffer: list[dict] = []
        self._loop_guard_stop_reason = None
        self._tool_budget_stop_reason = None
        self._user_message_written_this_turn = False

        # 钩子自己的两个状态（生命周期刻意相反，见模块 docstring 第 8 条）
        self._plan_validation_attempts = 0
        self._pending_plan_approval: dict | None = None

        self._wiki_prefetch = None
        self._wiki_prefetch_consumed = True
        self.message_queue = MessageQueue()
        self._auto_approve = False
        self.user_messages: list[str] = []

        self._plan_mode_manager = PlanModeManager(
            workspace=workspace, session_id=self.session_id)
        self.validation_calls = 0
        real_validate = self._plan_mode_manager.validate_plan_artifacts

        def _counting_validate():
            self.validation_calls += 1
            return real_validate()

        self._plan_mode_manager.validate_plan_artifacts = _counting_validate
        if permission_mode == "plan":
            self._plan_mode_manager.pre_plan_mode = "default"
            self._plan_mode_manager.plan_dir = self._plan_mode_manager.generate_plan_dir()
            if plan_content is not None:
                self.write_plan(plan_content)

    # ── 替身实现 ──

    def write_plan(self, content: str) -> None:
        (self._plan_mode_manager.plan_dir / "plan.md").write_text(content, encoding="utf-8")

    def abort_requested(self) -> bool:
        return False

    def auto_approve_enabled(self) -> bool:
        return self._auto_approve

    def append_user_message(self, content: str, snapshot_id: str | None = None) -> None:
        self.user_messages.append(content)
        self.session.append("user_message", {"content": content})

    def set_pending_plan_approval(self, entry: dict) -> None:
        self._pending_plan_approval = entry

    def take_pending_plan_approval(self, rpc_id: str) -> dict | None:
        entry = self._pending_plan_approval
        if entry is None or entry.get("rpc_id") != rpc_id:
            return None
        self._pending_plan_approval = None
        return entry

    def of_type(self, type_: str) -> list[dict]:
        return [e for e in self.session.events if e.get("type") == type_]


def _make_loop(agent: HandoffFakeAgent, monkeypatch, *, on_step=None):
    """把 run() 的模型调用与 turn 准备换成可控替身，返回 (loop, 计数器)。

    只桩 `_run_step`（模型调用）与 `_prepare_turn`（快照/wiki/记忆注入）——钩子本身、
    双队列 drain、收尾 break 全是真代码。
    """
    loop = AgentLoop(agent)

    async def _no_prepare(user_message):
        return None

    monkeypatch.setattr(loop, "_prepare_turn", _no_prepare)

    state = {"steps": 0}

    async def _fake_run_step():
        state["steps"] += 1
        if on_step is not None:
            on_step(state["steps"])
        return None, 0

    monkeypatch.setattr(loop, "_run_step", _fake_run_step)
    return loop, state


# ── 1. 合格 → 落 request + 回合真的结束 ────────────────────────────────────


async def test_valid_plan_at_turn_end_emits_approval_request_and_ends_turn(tmp_path, monkeypatch):
    agent = HandoffFakeAgent(tmp_path)
    loop, state = _make_loop(agent, monkeypatch)

    # 非阻塞的靶心断言：run() 必须**返回**。实现若改回阻塞式（就地等审批），
    # 这里会挂到 30s 超时变红——余量刻意给 30 而不是 3，全套压上来时事件循环会慢。
    await asyncio.wait_for(loop.run(None), 30)

    requests = agent.of_type("permission/request")
    assert len(requests) == 1
    req = requests[0]
    assert req["tool_name"] == "plan_approval"       # 哨兵值，前端靠它挑审批抽屉
    assert req["rpc_id"] == req["request_id"]        # 与权限门那条形状逐字段一致
    assert "clamp" in req["command"]                 # command 承载要审的计划全文
    assert agent._pending_plan_approval is not None
    assert agent._pending_plan_approval["rpc_id"] == req["rpc_id"]
    assert state["steps"] == 1                       # 只调了一次模型，没有续回合
    assert agent.user_messages == []                 # 合格时不喂任何修正消息


# ── 2/3. 不合格 → 喂回错误、自动续回合、且次数有界 ─────────────────────────


async def test_invalid_plan_feeds_errors_back_and_continues(tmp_path, monkeypatch):
    agent = HandoffFakeAgent(tmp_path, plan_content=BAD_PLAN)

    def on_step(n: int) -> None:
        if n == 2:
            agent.write_plan(GOOD_PLAN)   # 模型第二次把格式改对了

    loop, state = _make_loop(agent, monkeypatch, on_step=on_step)
    await asyncio.wait_for(loop.run(None), 30)

    assert state["steps"] == 2
    assert len(agent.user_messages) == 1
    fed = agent.user_messages[0]
    assert "任务清单" in fed and "- [ ] 1." in fed   # 错误清单可执行（含正确写法）
    assert str(agent._plan_mode_manager.plan_dir) in fed
    assert len(agent.of_type("permission/request")) == 1   # 修好后照常送审


async def test_validation_retry_is_bounded_at_two(tmp_path, monkeypatch):
    """连续不合格 → 自动修正**恰好** 2 次，第 3 次结束回合并给用户一份可见诊断。

    断言的是次数（== 2）而不是「至少 1 次」：把上限改成 3、或干脆去掉上限，
    这条都会红（steps 会变成 4 / 永远不返回）。
    """
    agent = HandoffFakeAgent(tmp_path, plan_content=BAD_PLAN)
    loop, state = _make_loop(agent, monkeypatch)
    await asyncio.wait_for(loop.run(None), 30)

    assert state["steps"] == 3
    assert len(agent.user_messages) == 2          # 喂回错误的次数 = 上限
    assert agent._plan_validation_attempts == 3   # 第三次判定后放弃
    assert agent.of_type("permission/request") == []   # 不合格绝不送审

    # 放弃时必须**用户可见**：只在 stderr 说 = 用户看到「模型写完了却什么都没发生」。
    diagnostics = [e for e in agent.of_type("assistant_message") if "未能送审" in e.get("content", "")]
    assert len(diagnostics) == 1
    assert "任务清单" in diagnostics[0]["content"]
    assert any("未能送审" in line for line in agent._turn_output_buffer)


# ── 4. 非 plan 模式：钩子严格短路（共享路径不能受影响）──────────────────────


async def test_non_plan_mode_is_untouched(tmp_path, monkeypatch):
    """spec §十.2 的靶心：这条收尾路径是**所有会话**共享的。

    断言连校验器都没被调用（validation_calls == 0），而不只是「没落事件」——
    「先校验再判模式」这种改法会留下事件为零但每个会话每回合都读一次盘的开销。
    """
    agent = HandoffFakeAgent(tmp_path, permission_mode="default")
    loop, state = _make_loop(agent, monkeypatch)
    await asyncio.wait_for(loop.run(None), 30)

    assert agent.validation_calls == 0
    assert agent.of_type("permission/request") == []
    assert agent.of_type("permission/resolved") == []
    assert agent.user_messages == []
    assert state["steps"] == 1


# ── 5. 校验器抛异常：不送审、不阻塞，但留可观测痕迹 ─────────────────────────


async def test_validator_exception_does_not_block_the_turn(tmp_path, monkeypatch):
    """C1 缺陷类的教训：失败方向必须是「照常结束回合」，但绝不能静默。"""
    agent = HandoffFakeAgent(tmp_path)

    def _boom():
        raise RuntimeError("validator exploded")

    agent._plan_mode_manager.validate_plan_artifacts = _boom
    loop, state = _make_loop(agent, monkeypatch)

    await asyncio.wait_for(loop.run(None), 30)   # 不阻塞、不抛出去

    assert agent.of_type("permission/request") == []
    assert state["steps"] == 1                   # 没有无限续回合
    # 留痕：stderr 之外还有一条进事件流，前端/评审集都看得见
    errors = agent.of_type("error")
    assert len(errors) == 1
    assert "validator exploded" in errors[0]["message"]


# ── 6. 自动审批开关已开 → 不落 request，当场 finalize ──────────────────────


async def test_auto_approve_on_at_turn_end_finalizes_without_request(tmp_path, monkeypatch):
    """spec §D9：开关已开时不凭空造请求（请求都没落盘，也就没有 resolved 要补）。"""
    import agents.agent_loop as agent_loop_module

    agent = HandoffFakeAgent(tmp_path)
    agent._auto_approve = True

    calls: list[tuple] = []

    def _fake_finalize(a, mgr, draft, choice):
        calls.append((a.session_id, choice, draft.full_plan[:20]))
        a.permission_mode = "acceptEdits"      # 真 finalize 会切模式，替身照做
        return "FINALIZED-BODY"

    monkeypatch.setattr(agent_loop_module, "_finalize_plan_exit", _fake_finalize)

    loop, state = _make_loop(agent, monkeypatch)
    await asyncio.wait_for(loop.run(None), 30)

    assert agent.of_type("permission/request") == []
    assert len(calls) == 1 and calls[0][1] == "execute"
    assert agent._pending_plan_approval is None
    assert state["steps"] == 2                 # continue 去执行，不是结束回合
    assert len(agent.user_messages) == 1
    msg = agent.user_messages[0]
    assert "FINALIZED-BODY" in msg             # 物化说明逐字沿用
    assert "当前权限模式" in msg and "task_list" in msg   # D8 的当前状态行
    for banned in ("已解除", "已恢复", "不再只读", "刚才"):
        assert banned not in msg, banned


# ── 7. supersede：新的合格计划作废旧的待审批 ────────────────────────────────


async def test_second_valid_plan_supersedes_the_pending_one(tmp_path, monkeypatch):
    """spec §十.3：本设计最容易漏的边界。

    漏了它，用户会批准一份已经不是当前内容的计划——对话框还开着（历史里那条 request
    没有配对的 resolved），而 plan.md 已经被改过。
    """
    agent = HandoffFakeAgent(tmp_path)
    loop, _ = _make_loop(agent, monkeypatch)

    await asyncio.wait_for(loop.run(None), 30)
    first = agent.of_type("permission/request")[0]
    assert agent._pending_plan_approval["rpc_id"] == first["rpc_id"]

    agent.write_plan(GOOD_PLAN.replace("clamp", "clamp2"))
    await asyncio.wait_for(loop.run(None), 30)

    resolved = agent.of_type("permission/resolved")
    assert [(r["rpc_id"], r["outcome"]) for r in resolved] == [
        (first["rpc_id"], "superseded")]
    requests = agent.of_type("permission/request")
    assert len(requests) == 2
    second = requests[1]
    assert second["rpc_id"] != first["rpc_id"]
    assert agent._pending_plan_approval["rpc_id"] == second["rpc_id"]
    assert "clamp2" in agent._pending_plan_approval["draft"].full_plan
    # 旧 rpc_id 已经取不出来了：批准一份作废的审批必须走不通
    assert agent.take_pending_plan_approval(first["rpc_id"]) is None


# ── 8. 两个状态的生命周期刻意相反 ──────────────────────────────────────────


def test_retry_counter_resets_but_pending_approval_survives_turn_reset(tmp_path):
    """`_plan_validation_attempts` 每回合归零，`_pending_plan_approval` 跨回合存活。

    归零：否则第二次规划一进来就撞在上限上，一次自我修正的机会都没有。
    存活：回合已经结束了、审批还没来——那正是非阻塞的定义，清掉它就等于把审批弄丢
    （前端还能从历史恢复出对话框，后端却再也认不出这个 rpc_id）。
    """
    agent = HandoffFakeAgent(tmp_path)
    agent._plan_validation_attempts = 2
    agent._pending_plan_approval = {"rpc_id": "abc", "draft": None, "choice_options": []}

    TurnRunner(agent)._reset_turn_state()

    assert agent._plan_validation_attempts == 0
    assert agent._pending_plan_approval == {"rpc_id": "abc", "draft": None, "choice_options": []}
    assert agent._current_turn == 1


def test_take_pending_plan_approval_is_idempotent_and_rpc_scoped(tmp_path):
    """取出即清空（双击/重试不会把同一批任务再物化一遍）；不匹配的 rpc_id 不清空
    （那个 id 可能属于危险命令确认那条阻塞路径，清掉就把它弄丢了）。"""
    agent = HandoffFakeAgent(tmp_path)
    entry = {"rpc_id": "r1", "draft": None, "choice_options": []}
    agent.set_pending_plan_approval(entry)

    assert agent.take_pending_plan_approval("other") is None
    assert agent._pending_plan_approval is entry       # 不匹配 → 不清空
    assert agent.take_pending_plan_approval("r1") is entry
    assert agent._pending_plan_approval is None
    assert agent.take_pending_plan_approval("r1") is None   # 第二次拿不到
