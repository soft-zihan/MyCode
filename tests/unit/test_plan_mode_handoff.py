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
from types import SimpleNamespace

import pytest

from agents.agent_loop import AgentLoop
from agents.core.session import Session
from agents.core.steer_queue import MessageQueue
from agents.core.turn_runner import TurnRunner
from agents.core.workspace import reset_workspace, set_workspace
from agents.plan.plan_mode import PlanModeManager
from agents.plan.plan_tool_executor import (
    finalize_approved_plan,
    record_plan_rejection,
)


@pytest.fixture(autouse=True)
def ws(tmp_path):
    """整个模块都在 tmp_path 的工作区作用域里跑。

    B2 之后本文件会跑真的 finalize_approved_plan：它要落 plan 系统条目
    （get_plans_dir）并物化进 task_list（task_store 的目录来自 get_workspace()
    这个 ContextVar）。不设作用域就会写进进程 CWD——那正是 W1 缺陷的形状。
    """
    token = set_workspace(tmp_path)
    yield tmp_path
    reset_workspace(token)


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

        # finalize_approved_plan 碰到的那几样（B2 起本文件跑真的 finalize）
        self._system_prompt = "PLAN-SYS"
        self._base_system_prompt = "BASE-SYS"
        self.context_cleared = 0
        self._context_manager = SimpleNamespace(
            clear_history_keep_system=lambda: setattr(self, "context_cleared",
                                                      self.context_cleared + 1))

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

    def _emit_permission_mode_event(self) -> None:
        self.session.append("permission/mode_changed", {
            "session_id": self.session.id,
            "mode": self.permission_mode,
        })

    def apply_pending_permission_mode(self) -> None:
        """agent_loop.run() 循环顶部（step 边界）会调它，替身必须提供。

        刻意是空实现：本文件钉的是送审与非阻塞审批，延迟模式切换由
        tests/unit/test_deferred_mode_switch.py 用**真 Agent** 钉住。
        """
        return None

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
    """spec §D9：开关已开时不凭空造请求（请求都没落盘，也就没有 resolved 要补）。

    跑的是**真的** finalize_approved_plan（不桩）：落库、切模式、清草稿、物化进
    task_list 全在断言里，桩掉就等于把这条链一起桩掉。
    """
    agent = HandoffFakeAgent(tmp_path)
    agent._auto_approve = True
    draft_dir = agent._plan_mode_manager.plan_dir

    loop, state = _make_loop(agent, monkeypatch)
    await asyncio.wait_for(loop.run(None), 30)

    assert agent.of_type("permission/request") == []
    assert agent._pending_plan_approval is None
    assert state["steps"] == 2                 # continue 去执行，不是结束回合
    assert agent.permission_mode == "acceptEdits"
    assert agent.session.plan_slug             # 落库真的发生了
    assert not draft_dir.exists()              # 草稿目录被清掉

    assert len(agent.user_messages) == 1
    msg = agent.user_messages[0]
    assert "条任务已物化进 task_list" in msg   # 物化说明逐字沿用（spec §D8）
    assert "当前权限模式" in msg and "task_list" in msg   # D8 的当前状态行在最前面
    assert msg.index("当前权限模式") < msg.index("User approved the plan")
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


# ══════════════════════════ B2：审批的解决路径 ══════════════════════════
#
# 工具删掉之后，「用户点了批准」这件事必须由后端把后续动作全部做完：落 resolved →
# finalize（退出 plan 模式 / 落库 / 物化 / 清草稿）→ 起一个新回合，其起始消息携带
# spec §D8 的当前状态声明。下面这组分两层：
#   - finalize_approved_plan / record_plan_rejection 的**消息措辞**（D8 靶心）；
#   - resolve_plan_approval 的**落地动作**（事件、模式、新回合）。


def _pending_for(agent: HandoffFakeAgent) -> dict:
    from agents.plan.plan_tool_executor import _validate_and_load_draft

    draft = _validate_and_load_draft(agent._plan_mode_manager)
    assert not isinstance(draft, str), draft
    return {"rpc_id": "r1", "draft": draft,
            "choice_options": ["execute", "manual-execute", "clear-and-execute",
                               "keep-planning"]}


@pytest.fixture
def resolve_env(tmp_path, monkeypatch):
    """把 resolve_plan_approval 的两个外部接缝换成可观测替身。

    - `_spawn_chat_task`：真的会 asyncio.create_task(agent.chat(...))，测试里没有
      模型可调。捕获参数即可——「起没起新回合、起始消息是什么」正是要断言的东西。
    - `resolve_session_workspace`：真实现会去查 session_manager 与 projcache，
      在测试里落到 Path.cwd()。钉成 tmp_path（与 autouse 的 ws 作用域同一个目录），
      于是 finalize 的落库/物化仍然写进隔离工作区。
    """
    from frontend.server.routers import plan_approval as pa

    spawned: list[str] = []
    monkeypatch.setattr(pa, "_spawn_chat_task",
                        lambda sm, agent, session, message: spawned.append(message))
    monkeypatch.setattr(pa, "resolve_session_workspace", lambda sid: tmp_path)
    return SimpleNamespace(module=pa, spawned=spawned,
                           sm=SimpleNamespace(register_chat_task=lambda *a, **k: None))


# ── D8 措辞：状态，不是转场 ────────────────────────────────────────────────


def test_execution_message_states_current_state_not_transition(tmp_path):
    """spec §D8 靶心：起始消息是**状态式**声明，不是转场叙述。

    为什么这条是硬约束（用户纠正过设计）：clear-and-execute 会清空上下文，模型根本
    没有那个「之前」，「已解除 / 已恢复 / 不再只读 / 刚才」指向的是它看不见的东西。
    把状态行改成任何一种转场措辞，这四个 banned 断言立刻红。
    """
    agent = HandoffFakeAgent(tmp_path)
    msg = finalize_approved_plan(agent, _pending_for(agent)["draft"], "execute")

    assert "当前权限模式" in msg and "task_list" in msg
    for banned in ("已解除", "已恢复", "不再只读", "刚才"):
        assert banned not in msg, banned
    # 状态行在**最前面**：它是模型读到的第一句（后面那段物化说明逐字沿用旧文案）
    assert msg.startswith("当前权限模式：acceptEdits")
    assert "条任务已物化进 task_list" in msg
    assert "exit_plan_mode" not in msg


def test_manual_execute_message_does_not_claim_unrestricted(tmp_path):
    """spec §D8：manual-execute 必须如实说「每次编辑仍需用户确认」，
    不能和 execute 共用一句「所有工具可用」——否则模型会以为可以连着改一批文件。"""
    agent = HandoffFakeAgent(tmp_path)
    msg = finalize_approved_plan(agent, _pending_for(agent)["draft"], "manual-execute")

    assert "确认" in msg
    assert "所有工具可用" not in msg          # 不共用 execute 那一行
    assert "当前权限模式：default" in msg      # 回 pre_plan_mode，如实报
    for banned in ("已解除", "已恢复", "不再只读", "刚才"):
        assert banned not in msg, banned


def test_clear_and_execute_message_is_self_contained(tmp_path):
    """清空上下文后模型对 plan 模式一无所知，这条消息是它唯一线索。

    断言它单独拿出来也能读出模式与工具状态（不依赖任何历史指代）——状态式声明天生
    满足这一点，因为它根本不引用历史。
    """
    agent = HandoffFakeAgent(tmp_path)
    msg = finalize_approved_plan(agent, _pending_for(agent)["draft"], "clear-and-execute")

    assert agent.context_cleared == 1          # 上下文真的被清了
    assert agent.permission_mode == "acceptEdits"
    assert "当前权限模式：acceptEdits" in msg
    assert "task_list" in msg and "in_progress" in msg and "completed" in msg
    assert "条任务已物化进 task_list" in msg
    for banned in ("已解除", "已恢复", "不再只读", "刚才"):
        assert banned not in msg, banned


def test_keep_planning_message_states_still_readonly(tmp_path):
    """拒绝路径也要陈述当前状态，否则模型会把「收到一条新消息」误读成「计划通过了」。
    用户意见原文必须在里面——那是模型修改计划的唯一输入。"""
    agent = HandoffFakeAgent(tmp_path)
    msg = record_plan_rejection(agent, "第 2 条任务的验收命令跑不起来")

    assert "仍在 plan 模式" in msg and "只读" in msg and "task_list" in msg
    assert "第 2 条任务的验收命令跑不起来" in msg
    assert agent.permission_mode == "plan"     # 一步都没动
    assert "exit_plan_mode" not in msg         # 旧文案那句「call exit_plan_mode again」没了
    for banned in ("已解除", "已恢复", "不再只读", "刚才"):
        assert banned not in msg, banned


# ── 解决路径的落地动作 ─────────────────────────────────────────────────────


def test_approve_finalizes_and_starts_execution_turn(tmp_path, resolve_env):
    """respond(allowed=True, choice="execute") 的全部后果。

    resolved 必须落盘：否则前端的「从历史恢复待审批」会把这个已批准的审批复活成
    幽灵对话框（上一轮踩过这条）。
    """
    agent = HandoffFakeAgent(tmp_path)
    pending = _pending_for(agent)
    draft_dir = agent._plan_mode_manager.plan_dir

    result = resolve_env.module.resolve_plan_approval(
        resolve_env.sm, agent, pending, allowed=True, choice="execute")

    assert result["success"] is True
    resolved = agent.of_type("permission/resolved")
    assert [(r["rpc_id"], r["outcome"]) for r in resolved] == [("r1", "allowed")]
    assert agent.permission_mode == "acceptEdits"
    assert agent.session.plan_slug                 # 落库
    assert not draft_dir.exists()                  # 清草稿
    assert len(resolve_env.spawned) == 1           # 起了新回合
    assert resolve_env.spawned[0].startswith("当前权限模式：acceptEdits")


@pytest.mark.parametrize("choice,expected_mode", [
    ("execute", "acceptEdits"),
    ("manual-execute", "default"),        # 回 pre_plan_mode（替身里设成 default）
    ("clear-and-execute", "acceptEdits"),
])
def test_three_approval_routes(tmp_path, resolve_env, choice, expected_mode):
    agent = HandoffFakeAgent(tmp_path)
    pending = _pending_for(agent)

    result = resolve_env.module.resolve_plan_approval(
        resolve_env.sm, agent, pending, allowed=True, choice=choice)

    assert result["success"] is True
    assert agent.permission_mode == expected_mode
    assert len(resolve_env.spawned) == 1
    assert agent.context_cleared == (1 if choice == "clear-and-execute" else 0)


def test_deny_keeps_plan_mode_and_delivers_feedback(tmp_path, resolve_env):
    agent = HandoffFakeAgent(tmp_path)
    pending = _pending_for(agent)

    result = resolve_env.module.resolve_plan_approval(
        resolve_env.sm, agent, pending, allowed=False, feedback="验收命令太弱")

    assert result["success"] is True
    assert [(r["rpc_id"], r["outcome"]) for r in agent.of_type("permission/resolved")] \
        == [("r1", "denied")]
    assert agent.permission_mode == "plan"        # 留在 plan 模式
    assert not agent.session.plan_slug            # 什么都没落库
    assert len(resolve_env.spawned) == 1
    msg = resolve_env.spawned[0]
    assert "验收命令太弱" in msg and "仍在 plan 模式" in msg


def test_finalize_failure_is_observable_and_does_not_start_a_turn(tmp_path, resolve_env, monkeypatch):
    """finalize 抛异常 → resolved 已落盘（不留幽灵对话框）、事件流里有一条 error、
    **不起新回合**、success=False。静默降级 + 上层成功信号是本项目最贵的缺陷类（C1）。"""
    agent = HandoffFakeAgent(tmp_path)
    pending = _pending_for(agent)

    def _boom(*a, **k):
        raise RuntimeError("integration exploded")

    monkeypatch.setattr(resolve_env.module, "finalize_approved_plan", _boom)

    result = resolve_env.module.resolve_plan_approval(
        resolve_env.sm, agent, pending, allowed=True, choice="execute")

    assert result["success"] is False
    assert "integration exploded" in result["message"]
    assert [(r["rpc_id"], r["outcome"]) for r in agent.of_type("permission/resolved")] \
        == [("r1", "allowed")]
    assert any("integration exploded" in e.get("message", "") for e in agent.of_type("error"))
    assert resolve_env.spawned == []


def test_auto_approve_toggle_resolves_pending_through_the_same_path(tmp_path, monkeypatch, resolve_env):
    """spec §D9 的交叉：审批**已挂着**才翻开开关 → 按「批准并自动执行」结掉，
    落 resolved{outcome:"auto"}。

    靶心是「共用同一个函数」：PUT 分支要是自己写一份 finalize，两条路必然漂移
    （例如一条起新回合、另一条不起）。这里断言它调的就是 resolve_plan_approval，
    且 outcome 是 "auto" 而不是 "allowed"——请求已落盘，决议必须落盘且如实标注成因。
    """
    from frontend.server.routers import sessions as sessions_router

    agent = HandoffFakeAgent(tmp_path)
    agent.set_pending_plan_approval(_pending_for(agent))

    calls: list[dict] = []
    real = resolve_env.module.resolve_plan_approval

    def _spy(sm, a, pending, **kw):
        calls.append(kw)
        return real(sm, a, pending, **kw)

    monkeypatch.setattr(resolve_env.module, "resolve_plan_approval", _spy)

    sessions_router._resolve_pending_plan_approval_as_auto(agent.session_id, agent)

    assert calls == [{"allowed": True, "choice": "execute", "outcome": "auto"}]
    assert [(r["rpc_id"], r["outcome"]) for r in agent.of_type("permission/resolved")] \
        == [("r1", "auto")]
    assert agent.permission_mode == "acceptEdits"
    assert len(resolve_env.spawned) == 1
    assert agent._pending_plan_approval is None


def test_auto_approve_toggle_without_pending_does_nothing(tmp_path, resolve_env):
    """没有待审批时翻开开关：只改属性，不落任何事件、不起任何回合。"""
    from frontend.server.routers import sessions as sessions_router

    agent = HandoffFakeAgent(tmp_path)
    sessions_router._resolve_pending_plan_approval_as_auto(agent.session_id, agent)

    assert agent.of_type("permission/resolved") == []
    assert resolve_env.spawned == []


# ── spec §十.4：CLI/headless 的就地问人回退 ─────────────────────────────────


async def test_cli_blocking_fallback_forks_on_plan_approval_fn(tmp_path, monkeypatch):
    """分叉判据 = `mgr.plan_approval_fn is not None`。

    ⚠️ spec 对这个判据的标注是反的（它写「注册了（前端服务）→ 非阻塞」），而全仓
    唯一的注册点是 agents/main.py:286 的交互式 CLI；前端服务器一个都不注册。所以
    「注册了」= 终端上坐着一个人 = 就地阻塞问人，「没注册」= 前端/headless = 落事件。
    照 spec 的字面标注实现会让 CLI 落一条没人应答的事件、审批永久挂着。
    """
    asked: list[str] = []

    async def approval_fn(plan_content: str) -> dict:
        asked.append(plan_content)
        return {"choice": "execute"}

    agent = HandoffFakeAgent(tmp_path)
    agent._plan_mode_manager.plan_approval_fn = approval_fn
    loop, state = _make_loop(agent, monkeypatch)
    await asyncio.wait_for(loop.run(None), 30)

    assert len(asked) == 1 and "clamp" in asked[0]
    assert agent.of_type("permission/request") == []   # 就地问人，不落事件
    assert agent._pending_plan_approval is None
    assert agent.permission_mode == "acceptEdits"      # 当场 finalize
    assert state["steps"] == 2                         # 同一个 run 里接着执行
    assert agent.user_messages[0].startswith("当前权限模式：acceptEdits")


async def test_cli_fallback_keep_planning_stays_in_plan_mode(tmp_path):
    async def approval_fn(plan_content: str) -> dict:
        return {"choice": "keep-planning", "feedback": "验收命令太弱"}

    agent = HandoffFakeAgent(tmp_path)
    agent._plan_mode_manager.plan_approval_fn = approval_fn
    loop = AgentLoop(agent)

    assert await loop._plan_mode_handoff() is True    # 继续本回合让模型改
    assert agent.permission_mode == "plan"
    assert agent.of_type("permission/request") == []
    msg = agent.user_messages[0]
    assert "验收命令太弱" in msg and "仍在 plan 模式" in msg


async def test_frontend_path_still_emits_the_event(tmp_path, monkeypatch):
    """反面：plan_approval_fn 没注册（前端/headless）→ 必须走非阻塞事件路径。

    与上一条一起钉住分叉判据的两个方向——只测一边，「判据写反」这种改法不会红。
    """
    agent = HandoffFakeAgent(tmp_path)
    assert agent._plan_mode_manager.plan_approval_fn is None
    loop, _ = _make_loop(agent, monkeypatch)
    await asyncio.wait_for(loop.run(None), 30)

    assert len(agent.of_type("permission/request")) == 1
    assert agent.permission_mode == "plan"     # 没被就地 finalize


# ── 删除面 ─────────────────────────────────────────────────────────────────


def test_exit_plan_mode_is_gone_from_every_backend_surface():
    """逐个断言工具真的不在了，而不是「声明删了、分支还留着」。"""
    import agents.plan.plan_tool_executor as pte
    from agents.tools.dispatcher import HUMAN_WAIT_TOOLS, ToolDispatcher
    from agents.tools.permissions import check_permission
    from agents.tools.registry import TOOL_EXECUTION_MODES, tool_definitions

    names = [t["name"] for t in tool_definitions]
    assert "exit_plan_mode" not in names
    assert "enter_plan_mode" in names          # 只删 exit，enter 保留
    assert "exit_plan_mode" not in TOOL_EXECUTION_MODES

    # ask_user 必须**留在** HUMAN_WAIT_TOOLS：它修的是与本设计无关的真缺陷
    # （外层默认 60s 会先于它自己的 300s 内部上限把它掐死）。
    assert HUMAN_WAIT_TOOLS == frozenset({"ask_user"})
    assert ToolDispatcher(agent_ref=SimpleNamespace()).get_tool_timeout("ask_user") is None

    for symbol in ("_exit_plan_mode", "_request_approval", "_finalize_plan_exit"):
        assert not hasattr(pte, symbol), symbol
    assert hasattr(pte, "finalize_approved_plan")

    # enter 的放行口还在（权限层仍是唯一防线，spec §D5）
    assert check_permission("enter_plan_mode", {}, "plan")["action"] == "allow"
