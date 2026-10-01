"""软验收闸门：从事件日志观察证据，不接受 agent 自报。"""
from __future__ import annotations

import pytest

from agents.core.session import Session
from agents.tools.task_gate import build_acceptance_warning, has_successful_shell_since
from agents.tools.task_store import TaskItem


@pytest.fixture
def session():
    s = Session("gate-s", origin="sub_agent")
    s.system_prompt = "SYS"
    return s


def _round(session, outcome="success", name="run_shell"):
    """一轮完整的 assistant(tool_calls) + tool_result，返回 tool_result 的 seq。"""
    call_id = f"c{len(session.events)}"
    session.append("assistant_message", {"content": "", "tool_calls": [
        {"id": call_id, "type": "function",
         "function": {"name": name, "arguments": "{}"}}]})
    session.append("tool_result_msg", {
        "call_id": call_id, "content": "out", "tool_name": name, "outcome": outcome,
    })
    return session.events[-1]["seq"]


def test_no_events_means_no_evidence(session):
    assert has_successful_shell_since(session, None) is False


def test_successful_shell_counts(session):
    _round(session, "success")
    assert has_successful_shell_since(session, None) is True


def test_failed_shell_does_not_count(session):
    _round(session, "error")
    assert has_successful_shell_since(session, None) is False


def test_cancelled_shell_does_not_count(session):
    _round(session, "cancelled")
    assert has_successful_shell_since(session, None) is False


def test_empty_outcome_does_not_count(session):
    """取消路径落盘的 outcome 是空串，不能被当成成功。"""
    _round(session, "")
    assert has_successful_shell_since(session, None) is False


def test_non_shell_tool_does_not_count(session):
    _round(session, "success", name="write_file")
    assert has_successful_shell_since(session, None) is False


def test_legacy_events_without_the_keys_do_not_break_the_gate(session):
    """commit 4f0b5ac 之前落盘的事件既没有 tool_name 也没有 outcome。

    是**键缺席**，不是空串——所以两个键都必须用 `.get(k, "")` 读。磁盘上的旧
    会话照样要能过闸门：不抛、不把「读不出来」误报成「有证据」。
    """
    session.append("assistant_message", {"content": "", "tool_calls": [
        {"id": "c0", "type": "function",
         "function": {"name": "run_shell", "arguments": "{}"}}]})
    session.append("tool_result_msg", {"call_id": "c0", "content": "out"})
    legacy = session.events[-1]
    assert "outcome" not in legacy and "tool_name" not in legacy   # 前提：键确实缺席
    assert has_successful_shell_since(session, None) is False
    # 旧事件之后来了新格式的成功记录，闸门照常看得见（不被旧事件卡住）
    _round(session, "success")
    assert has_successful_shell_since(session, None) is True


def test_events_at_or_before_since_seq_are_ignored(session):
    seq = _round(session, "success")
    assert has_successful_shell_since(session, seq) is False       # 严格大于
    assert has_successful_shell_since(session, seq - 1) is True


def test_since_seq_none_scans_everything(session):
    _round(session, "success")
    assert has_successful_shell_since(session, None) is True


def test_success_after_a_failure_still_counts(session):
    _round(session, "error")
    _round(session, "success")
    assert has_successful_shell_since(session, None) is True


def test_hidden_events_still_count(session):
    """折叠隐藏了证据事件，但证据确实发生过 —— 扫 session.events 而非可见集。

    否则一次折叠会让已完成的任务突然「变得没验证过」，闸门开始对无辜的任务报警，
    而误报正是会训练模型忽略警告的那种失败。
    """
    seq = _round(session, "success")
    session.hide_events([seq])
    assert seq not in session.visible_seqs      # 确实被隐藏了
    assert has_successful_shell_since(session, None) is True


def test_warning_names_the_acceptance_command():
    task = TaskItem(id=2, content="跑测试", acceptance="pytest tests/x.py -q")
    warn = build_acceptance_warning(task)
    assert "pytest tests/x.py -q" in warn
    assert "#2" in warn


def test_warning_is_not_an_accusation():
    """文案必须留出「已经验证过」的余地，否则模型会学会忽略它。"""
    warn = build_acceptance_warning(TaskItem(id=1, content="x", acceptance="pytest"))
    assert "如果" in warn or "若" in warn


def test_warning_mentions_how_to_fix():
    warn = build_acceptance_warning(TaskItem(id=1, content="x", acceptance="pytest -q"))
    assert "pytest -q" in warn      # 修复动作就是跑那条命令，文案里要能直接抄
