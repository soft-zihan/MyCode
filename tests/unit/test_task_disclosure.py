"""披露注入测试。用真实 Session，且**确实落盘**（此前这里写的是「纯内存，不落盘」，是错的）。

`Session.append` 每次都会走 `_persist_event` → `backend.append`（agents/core/session.py），
`add_task`/`update_task` 也会 `save_tasks` 写 store 文件——两条写入路径都真落盘，只是落在
`ws` fixture 的 `tmp_path` 工作区里，所以测试之间互不污染、也碰不到真实用户数据。
这是好事：`memory_injection` 这个事件类型在 jsonl 与 sqlite 两条后端腿上都被真实持久化过。
"""
from __future__ import annotations

import pytest

from agents.core.circuit_breaker import llm_circuit_breaker
from agents.core.model_caller import ModelCaller
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


def test_add_with_detail_and_seq_records_origin(ws):
    """I1：`add(detail=...)` 必须记账 origin seq，否则造出的正是本设计要消灭的
    那次纯重复注入。

    模型自己写的 detail 正躺在承载这次 tool_calls 的 assistant 事件里，折叠前
    一直可见；`add_task` 没有 current_seq 参数时 detail_origin_seq 停在 None，
    而 needs_disclosure 对 None 无条件返回 True（实测：一条 assistant 消息里
    add 三个带 detail 的任务、该消息仍可见，下一次调用注入了 465 字符与
    tool_calls 参数逐字相同的内容）。与已被 bless 的 `update(detail=...)`
    重指向裁定对称。
    """
    s = _session()
    seq = s.append("assistant_message", {"content": "", "tool_calls": [
        {"id": "c1", "type": "function",
         "function": {"name": "task_list",
                      "arguments": '{"operation":"add","detail":"方案A"}'}}]})["seq"]

    add_task("s1", "A", detail="方案A", current_seq=seq)

    assert list_tasks("s1")[0].detail_origin_seq == seq
    assert ensure_focus_detail_visible(s) is False      # 承载事件仍可见 → 不注入


def test_add_with_detail_and_no_seq_leaves_origin_none(ws):
    """I1 的对照面 = Plan 3 物化路径：不传 current_seq 就保持 None、保持会注入。

    物化出来的 detail 来自磁盘上的 tasks.md，**不在**模型自己的 tool_calls 里，
    所以首条无条件注入正是它想要的。物化调用 add_task 时不传 current_seq，这条
    测试钉住该调用形状不被上面的记账改动顺手改掉。
    """
    s = _session()
    add_task("s1", "A", detail="物化方案")

    assert list_tasks("s1")[0].detail_origin_seq is None
    assert ensure_focus_detail_visible(s) is True


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


def test_non_int_seq_is_reported_not_silent(ws, monkeypatch):
    """M10：不可能发生的分支也要响一声。

    session.py 对非 SSE 事件总是写 int seq，所以这条路到不了；但静默的代价是
    「不记账 → 下次请求重注入最多 6000 字符」且不留任何痕迹。返回值仍是 True
    ——注入确实发生了，本轮模型看得到 detail。
    """
    add_task("s1", "A", detail="方案A")
    logged: list[str] = []
    monkeypatch.setattr("agents.tools.task_disclosure.print_error", logged.append)

    s = _session()
    real_append = s.append

    def _append_dropping_seq(event_type, data):
        event = real_append(event_type, data)
        return {k: v for k, v in event.items() if k != "seq"}

    s.append = _append_dropping_seq

    assert ensure_focus_detail_visible(s) is True           # 注入仍算成功
    assert list_tasks("s1")[0].detail_origin_seq is None    # 确实没记账
    assert len(logged) == 1
    assert "seq" in logged[0]


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


# --- 调用点接线（I2）--------------------------------------------------------
#
# `model_caller._attempt` 里那次调用是 ensure_focus_detail_visible 的**唯一**生产
# 调用点（repo-wide grep 确认）。此前没有任何测试驱动 ModelCaller.call/_attempt，
# 于是把那个调用连同它的 try/except 守卫一起删掉，全套测试仍然全绿，而整个推式层
# 已经消失——本分支第三次出现「承重不变量只由散文守着」。
#
# 形状：记录器打在 model_caller 命名空间里的 ensure_focus_detail_visible 上，哨兵
# 打在 _assemble_request 上（同步方法，抛异常即短路）。于是既不需要伪造异步 chunk
# 流（_consume_stream 根本不会执行），又能用「记录器先于哨兵」一条断言同时钉住两件
# 事：调用点存在，且它在装配之前——后者正是设计依赖的顺序（落在装配之后，本次请求
# 就看不到刚注入的 detail）。刻意不用 inspect.getsource 做源码文本断言：那种检查
# 脆且 repo 里无先例。


class _WiringAgent:
    """ModelCaller.call/_attempt 真正读到的全部属性（照 model_caller.py 读出来的）。

    call 读 model / _current_turn / _current_step / is_sub_agent（trace_span 的
    metadata），_attempt 读 session（快照加载）与 check_and_compact。哨兵短路在装配
    处，所以 messages / openai_client / _system_prompt_breakdown 这些一概不需要。

    check_and_compact 的 tasks 形参是 Plan 3b Task B1 的每请求快照：_attempt 读一次、
    显式传给三个消费者。这里记下来，好让下面的测试能断言它真的被传了。
    """

    def __init__(self, session, order: list):
        self.session = session
        self.model = "test-model"
        self._current_turn = 1
        self._current_step = 0
        self.is_sub_agent = False
        self._order = order
        self.snapshot = "__unset__"

    async def check_and_compact(self, tasks=None):
        self.snapshot = tasks
        self._order.append("check_and_compact")


def _sentinel_assemble(order: list):
    def _assemble(self, span, tools_enabled, tool_choice=None, tasks=None):
        order.append("assemble")
        raise RuntimeError("sentinel: 短路在装配处")
    return _assemble


@pytest.fixture
def _reset_circuit_breaker():
    """with_retry 会向模块级熔断器记一次失败；不复位就把它漏给后续测试。"""
    yield
    llm_circuit_breaker.record_success()


async def test_disclosure_call_site_fires_before_request_assembly(
    ws, monkeypatch, _reset_circuit_breaker
):
    order: list = []
    session = _session()
    snapshots: list = []

    def _recorder(sess, tasks=None):
        order.append("disclosure")
        assert sess is session        # 传的是 session 本体，不是 session.id
        snapshots.append(tasks)
        return False

    monkeypatch.setattr(
        "agents.core.model_caller.ensure_focus_detail_visible", _recorder)
    monkeypatch.setattr(ModelCaller, "_assemble_request", _sentinel_assemble(order))

    agent = _WiringAgent(session, order)
    with pytest.raises(RuntimeError, match="sentinel"):
        await ModelCaller(agent).call()

    # check_and_compact 在前（折叠可能刚把上一次披露隐藏掉），装配在最后
    assert order == ["check_and_compact", "disclosure", "assemble"]
    # 两个消费者拿到的是**同一个**快照对象（每请求只读一次的那一次读的产物）。
    # 快照的完整语义（读次数、S 看到的内容、探针的惰性）在 test_task_snapshot.py。
    assert snapshots == [agent.snapshot]


async def test_disclosure_failure_does_not_block_the_model_call(
    ws, monkeypatch, _reset_circuit_breaker
):
    """守卫的另一半：披露抛异常也要继续走到装配（绝不能阻断模型调用）。"""
    order: list = []
    session = _session()

    def _boom(sess, tasks=None):
        order.append("disclosure")
        raise AttributeError("'int' object has no attribute 'strip'")

    monkeypatch.setattr(
        "agents.core.model_caller.ensure_focus_detail_visible", _boom)
    monkeypatch.setattr(ModelCaller, "_assemble_request", _sentinel_assemble(order))

    with pytest.raises(RuntimeError, match="sentinel"):
        await ModelCaller(_WiringAgent(session, order)).call()

    assert order == ["check_and_compact", "disclosure", "assemble"]


async def test_call_site_really_injects_the_focus_detail(
    ws, monkeypatch, _reset_circuit_breaker
):
    """不用替身：真 ensure_focus_detail_visible 经调用点注入到 agent.session。

    上面两条把函数换成了记录器，只证明「有个调用点」；这一条证明那个调用点传的
    session 是对的、注入真的落盘、并且记了账（否则每次模型调用都会重注入）。
    """
    add_task("s1", "A", detail="方案A")
    session = _session()
    order: list = []
    monkeypatch.setattr(ModelCaller, "_assemble_request", _sentinel_assemble(order))

    with pytest.raises(RuntimeError, match="sentinel"):
        await ModelCaller(_WiringAgent(session, order)).call()

    injected = [e for e in session.events if e.get("type") == "memory_injection"]
    assert len(injected) == 1
    assert "方案A" in injected[0]["content"]
    assert list_tasks("s1")[0].detail_origin_seq == injected[0]["seq"]

