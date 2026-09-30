"""S 常驻尾部 ephemeral 通道。

S 走尾部而非主 system prompt：`system[0]` 会话内真冻结（U5a，
prompt_runtime.refresh_runtime_system_prompt），任何重建都使整个 prefix cache
失效。尾部消息追加在历史之后，所以每请求都变但不使前缀失效，只让自己那部分
无法命中——这正是常驻块该付的成本形态（spec §六 三层可见性）。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from agents.core.prompt_runtime import build_tail_system_messages
from agents.core.workspace import reset_workspace, set_workspace
from agents.tools.task_store import add_task, update_task


@pytest.fixture
def ws(tmp_path):
    token = set_workspace(tmp_path)
    yield tmp_path
    reset_workspace(token)


def _agent(session_id="s1", permission_mode="default"):
    """build_tail_system_messages 读取的全部属性。

    漏一个就是 AttributeError 而不是在测东西——加尾部贡献者时记得同步这里。
    """
    return SimpleNamespace(
        _custom_system_prompt=None,
        permission_mode=permission_mode,
        _plan_mode_manager=None,
        estimated_context_tokens=1000,
        effective_window=100000,
        _fold_last_time=0.0,
        _tool_error_streak=0,
        _same_tool_repeat_count=0,
        session=SimpleNamespace(id=session_id),
    )


def test_no_task_block_when_list_empty(ws):
    tails = build_tail_system_messages(_agent())
    assert not any("Task List" in t for t in tails)


def test_task_block_present_when_list_non_empty(ws):
    add_task("s1", "重构 X", acceptance="pytest -k x")
    tails = build_tail_system_messages(_agent())
    block = next(t for t in tails if "Task List" in t)
    assert "重构 X" in block
    assert "pytest -k x" in block


def test_task_block_never_contains_detail(ws):
    """常驻块里有 detail 就等于放弃渐进式披露——全量方案又变成常驻的了。"""
    add_task("s1", "重构 X", detail="一大段不该常驻的方案")
    tails = build_tail_system_messages(_agent())
    assert all("一大段不该常驻的方案" not in t for t in tails)


def test_task_block_comes_before_fold_guidance(ws):
    """S 只在任务变更时变，利用率百分比每请求都变——更易变的放最后。"""
    add_task("s1", "重构 X")
    tails = build_tail_system_messages(_agent())
    task_idx = next(i for i, t in enumerate(tails) if "Task List" in t)
    fold_idx = next(i for i, t in enumerate(tails) if "Runtime Fold Guidance" in t)
    assert task_idx < fold_idx


def test_completed_items_drop_out_of_block(ws):
    a = add_task("s1", "A")
    add_task("s1", "B")
    update_task("s1", a.id, status="completed")
    block = next(t for t in build_tail_system_messages(_agent()) if "Task List" in t)
    assert "1/2 done" in block
    assert "#1" not in block  # 已完成条目不逐条列出，只进计数
    assert "#2" in block


def test_custom_system_prompt_suppresses_all_tails(ws):
    add_task("s1", "重构 X")
    agent = _agent()
    agent._custom_system_prompt = "自定义"
    assert build_tail_system_messages(agent) == []


def test_plan_mode_tail_still_first_when_in_plan_mode(ws):
    add_task("s1", "重构 X")
    agent = _agent(permission_mode="plan")
    agent._plan_mode_manager = SimpleNamespace(
        build_plan_mode_prompt=lambda: "# Plan Mode Active\n写 tasks.md"
    )
    tails = build_tail_system_messages(agent)
    assert "Plan Mode Active" in tails[0]
    task_idx = next(i for i, t in enumerate(tails) if "Task List" in t)
    assert task_idx > 0
