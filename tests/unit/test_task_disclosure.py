"""披露注入测试。用真实 Session（纯内存，不落盘）。"""
from __future__ import annotations

import pytest

from agents.core.session import Session
from agents.core.workspace import reset_workspace, set_workspace
from agents.tools.task_disclosure import ensure_focus_detail_visible
from agents.tools.task_store import add_task, list_tasks, mark_detail_disclosed, update_task


@pytest.fixture
def ws(tmp_path):
    token = set_workspace(tmp_path)
    yield tmp_path
    reset_workspace(token)


def _session() -> Session:
    # origin="sub_agent" 让会话落进 DERIVED_SESSION_ORIGINS（session.py），不进用户
    # 会话列表/清理/最近会话——与 test_context_compression.py:68 等现有测试同一约定
    s = Session("s1", origin="sub_agent")
    s.system_prompt = "SYS"
    return s


def test_no_injection_when_list_empty(ws):
    assert ensure_focus_detail_visible(_session()) is False


def test_no_injection_when_focus_has_no_detail(ws):
    add_task("s1", "A")
    assert ensure_focus_detail_visible(_session()) is False


def test_injects_when_focus_detail_never_disclosed(ws):
    add_task("s1", "A", detail="方案A")
    s = _session()
    assert ensure_focus_detail_visible(s) is True
    injected = [e for e in s.events if e.get("type") == "memory_injection"]
    assert len(injected) == 1
    assert "方案A" in injected[0]["content"]
    assert "<system-reminder>" in injected[0]["content"]


def test_injection_records_origin_seq(ws):
    item = add_task("s1", "A", detail="方案A")
    s = _session()
    ensure_focus_detail_visible(s)
    assert list_tasks("s1")[0].detail_origin_seq is not None


def test_second_call_is_idempotent(ws):
    add_task("s1", "A", detail="方案A")
    s = _session()
    assert ensure_focus_detail_visible(s) is True
    assert ensure_focus_detail_visible(s) is False
    assert ensure_focus_detail_visible(s) is False
    injected = [e for e in s.events if e.get("type") == "memory_injection"]
    assert len(injected) == 1


def test_reinjects_after_origin_event_is_folded_away(ws):
    add_task("s1", "A", detail="方案A")
    s = _session()
    ensure_focus_detail_visible(s)
    first_seq = list_tasks("s1")[0].detail_origin_seq

    s.hide_events([first_seq])  # 模拟折叠把披露事件隐藏掉

    assert ensure_focus_detail_visible(s) is True
    injected = [e for e in s.events if e.get("type") == "memory_injection"]
    assert len(injected) == 2


def test_no_reinjection_while_origin_event_still_visible(ws):
    add_task("s1", "A", detail="方案A")
    s = _session()
    ensure_focus_detail_visible(s)
    s.append("user_message", {"content": "继续"})  # 无关事件不该触发重注入
    assert ensure_focus_detail_visible(s) is False


def test_focus_moves_to_next_task_after_completion(ws):
    a = add_task("s1", "A", detail="方案A")
    add_task("s1", "B", detail="方案B")
    s = _session()
    ensure_focus_detail_visible(s)
    assert "方案A" in [e for e in s.events if e.get("type") == "memory_injection"][0]["content"]

    update_task("s1", a.id, status="in_progress", current_seq=s.append(
        "user_message", {"content": "开工"})["seq"])
    update_task("s1", a.id, status="completed")

    assert ensure_focus_detail_visible(s) is True
    injected = [e for e in s.events if e.get("type") == "memory_injection"]
    assert "方案B" in injected[-1]["content"]


def test_failed_task_detail_is_disclosed_with_error(ws):
    a = add_task("s1", "A", detail="方案A")
    update_task("s1", a.id, status="failed", error="AssertionError: 3 != 4")
    s = _session()
    assert ensure_focus_detail_visible(s) is True
    content = [e for e in s.events if e.get("type") == "memory_injection"][0]["content"]
    assert "AssertionError: 3 != 4" in content


def test_ui_style_detail_edit_triggers_reinjection(ws):
    """人在 UI 改 detail → 清空 detail_origin_seq → 下次注入新内容。"""
    add_task("s1", "A", detail="旧方案")
    s = _session()
    ensure_focus_detail_visible(s)

    update_task("s1", 1, detail="人改过的新方案")
    mark_detail_disclosed("s1", 1, -1)  # -1 不在 visible_seqs 里，等价于「已失效」

    assert ensure_focus_detail_visible(s) is True
    injected = [e for e in s.events if e.get("type") == "memory_injection"]
    assert "人改过的新方案" in injected[-1]["content"]


def test_long_detail_is_clipped_in_injection(ws):
    add_task("s1", "A", detail="z" * 9000)
    s = _session()
    ensure_focus_detail_visible(s)
    content = [e for e in s.events if e.get("type") == "memory_injection"][0]["content"]
    assert "已截断" in content
    assert list_tasks("s1")[0].detail == "z" * 9000  # store 里是全量
