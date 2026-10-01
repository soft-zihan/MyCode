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
    """常驻块里有 detail 就等于放弃渐进式披露——全量方案又变成常驻的了。

    必须先正向证明 S 在尾部里：否则「S 被 prompt_runtime 的 try/except 吞掉」或
    「S 压根没渲染」都能让下面那条负向断言平凡为真，而这条测试是整份设计里最
    吃重的那条性质的端到端钉子。
    """
    add_task("s1", "重构 X", detail="一大段不该常驻的方案")
    tails = build_tail_system_messages(_agent())
    assert any("重构 X" in t for t in tails)                  # 先证明 S 真的在
    assert all("一大段不该常驻的方案" not in t for t in tails)   # 再证明它不含 detail


def test_task_block_comes_before_fold_guidance(ws):
    """S 只在任务变更时变，利用率百分比每请求都变——更易变的放最后。"""
    add_task("s1", "重构 X")
    tails = build_tail_system_messages(_agent())
    task_idx = next(i for i, t in enumerate(tails) if "Task List" in t)
    fold_idx = next(i for i, t in enumerate(tails) if "Runtime Fold Guidance" in t)
    assert task_idx < fold_idx


def test_s_block_is_appended_after_the_conversation_history(ws):
    """上一条钉不到的另一半：S 相对**对话历史**的位置。

    `build_tail_system_messages` 只返回一个 list，「它落在哪儿」由调用方决定：
    model_caller._assemble_request 在 `a.messages`（历史）之后 append（:310-311）。
    若改成插到历史之前/中间，主 system prompt 之后那段前缀就每请求都变、prefix
    cache 整段失效——常驻块该付的成本形态（「只让自己那部分无法命中」）会变成
    「每请求把全部历史重新计费」。这是 task-list-context-analysis §6 那条成本
    不变量的一半，此前只有散文守着。
    """
    from agents.core.model_caller import ModelCaller

    add_task("s1", "重构 X", acceptance="pytest -k x")
    stub = _agent()                       # 真渲染 S 的那份桩，不是替身字符串
    history = [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "干活"},
        {"role": "assistant", "content": "好"},
    ]
    agent = SimpleNamespace(
        tools=[],
        messages=list(history),
        model="test-model",
        thinking=None,
        build_tail_system_messages=lambda tasks=None: build_tail_system_messages(stub, tasks),
    )
    span = SimpleNamespace(update=lambda **_kw: None)

    params, _raw, _metrics = ModelCaller(agent)._assemble_request(
        span, tools_enabled=False)

    msgs = params["messages"]
    assert [m["content"] for m in msgs[:len(history)]] == ["SYS", "干活", "好"]
    tail_idx = [i for i, m in enumerate(msgs) if "Task List" in str(m.get("content", ""))]
    assert tail_idx, "S 没被渲染出来，这条测试就什么都没钉住"
    # 全部尾部块（含 S）都在历史之后：把 append 改成 insert(0, ...) 或插到 system
    # 之后，这里就红。
    assert min(tail_idx) >= len(history)
    assert all(m["role"] == "system" for m in msgs[len(history):])


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
