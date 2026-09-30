"""披露注入测试。用真实 Session，且**确实落盘**（此前这里写的是「纯内存，不落盘」，是错的）。

`Session.append` 每次都会走 `_persist_event` → `backend.append`（agents/core/session.py），
`add_task`/`update_task` 也会 `save_tasks` 写 store 文件——两条写入路径都真落盘，只是落在
`ws` fixture 的 `tmp_path` 工作区里，所以测试之间互不污染、也碰不到真实用户数据。
这是好事：`memory_injection` 这个事件类型在 jsonl 与 sqlite 两条后端腿上都被真实持久化过。
"""
from __future__ import annotations

import pytest

from agents.core.session import Session
from agents.core.workspace import reset_workspace, set_workspace
from agents.tools.task_disclosure import ensure_focus_detail_visible
from agents.tools.task_store import add_task, list_tasks, mark_detail_disclosed, update_task
# 工具调用配对断言：repo 里已有唯一一份实现（折叠测试在用），直接复用而不是抄一份，
# 免得两处判据漂移。tests/unit/__init__.py 存在 + pytest.ini 的 pythonpath=. 使这条
# 绝对导入可用。
from tests.unit.test_context_compression import _assert_valid_tool_pairs


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
    add_task("s1", "A", detail="方案A")
    s = _session()
    ensure_focus_detail_visible(s)
    injected = [e for e in s.events if e.get("type") == "memory_injection"]
    assert len(injected) == 1
    # 断言相等而不是 `is not None`：记错 seq（0 / -1 / 上一条事件的 seq）正是
    # isinstance(seq, int) 守卫要防的那一类 bug，`is not None` 会放过它。
    assert list_tasks("s1")[0].detail_origin_seq == injected[0]["seq"]


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
    """人在 UI 改 detail → `update_task` 不带 current_seq → detail_origin_seq=None → 重注入。

    不写 `mark_detail_disclosed(..., -1)` 这类哨兵：生产代码从不写 -1，用它当触发条件
    会让本测试在 `update_task` 停止重置 detail_origin_seq 之后**依旧通过**，即测不到它
    名字里那条路径。断言留在 injected[-1] 上，证明披露块是按 store 里的实时内容重新
    渲染的，而不是拿第一次的块缓存复用。
    """
    add_task("s1", "A", detail="旧方案")
    s = _session()
    ensure_focus_detail_visible(s)

    update_task("s1", 1, detail="人改过的新方案")
    assert list_tasks("s1")[0].detail_origin_seq is None

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


def test_injection_never_splits_tool_pair_and_reaches_the_request(ws):
    """本特性唯一的硬约束：注入既不能拆散工具调用配对，又必须真的进本次请求。

    其余测试全部断言 `s.events`（原始事件日志），看不到模型真正收到的东西；这一条
    走 `get_messages_for_llm()`（请求路径，agents/core/session.py 的派生），一次
    覆盖两半：
    - 顺序安全靠的是一个**位置**事实——注入事件的 seq 大于上一条 `tool_result_msg`
      的 seq，而派生按 visible-seq 顺序进行。这里用 assistant(tool_calls) → tool
      的真实配对把该事实钉住，`_assert_valid_tool_pairs` 在 user 消息插进配对中间
      时会失败。
    - `msgs[-1]` 含 detail，证明注入没被派生丢掉/折叠掉。

    Task 11 会改这块附近的代码，这是它的回归闸门。
    """
    add_task("s1", "A", detail="方案A")
    s = _session()
    s.append("assistant_message", {"content": "", "tool_calls": [
        {"id": "c1", "type": "function",
         "function": {"name": "task_list", "arguments": "{}"}}]})
    s.append("tool_result_msg", {"call_id": "c1", "content": "ok"})

    assert ensure_focus_detail_visible(s) is True

    msgs = s.get_messages_for_llm()
    _assert_valid_tool_pairs(msgs)          # 顺序不破工具调用配对
    assert "方案A" in msgs[-1]["content"]    # 且真的进了本次请求


def test_bookkeeping_failure_rolls_back_the_injection(ws, monkeypatch):
    """记账失败必须回滚注入，否则事件留在可见集里而 detail_origin_seq 未记录，
    此后**每次**模型调用都重注入最多 6000 字符——本特性唯一一处无上界的浪费，
    而消除这类浪费正是条件披露设计存在的理由。
    """
    add_task("s1", "A", detail="方案A")
    s = _session()

    calls = {"n": 0}

    def _flaky_mark(session_id, task_id, seq):
        # 只让第一次记账失败（模拟 save_tasks 的 path.write_text 抛错），
        # 之后转调真实现，以便顺带验证回滚不会把特性卡死。
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("save_tasks: simulated write failure")
        return mark_detail_disclosed(session_id, task_id, seq)

    # task_disclosure 用 `from ... import mark_detail_disclosed` 绑了本地名，
    # 所以要打在 task_disclosure 的命名空间上，打 task_store 的没用。
    monkeypatch.setattr(
        "agents.tools.task_disclosure.mark_detail_disclosed", _flaky_mark
    )

    assert ensure_focus_detail_visible(s) is False

    # 事件仍在日志里（append-only，不回退写入），但已从可见集摘掉
    injected = [e for e in s.events if e.get("type") == "memory_injection"]
    assert len(injected) == 1
    assert injected[0]["seq"] not in s.visible_seqs
    # 模型看得见的消息里没有披露块
    msgs = s.get_messages_for_llm()
    assert not any("方案A" in (m.get("content") or "") for m in msgs)
    # 两个状态没有分叉：detail_origin_seq 仍是 None
    assert list_tasks("s1")[0].detail_origin_seq is None

    # 回滚后特性自愈：下一次调用（记账已恢复）照常注入并记上正确的 seq
    assert ensure_focus_detail_visible(s) is True
    assert list_tasks("s1")[0].detail_origin_seq in s.visible_seqs
