"""等人应答的工具不设外层超时（HUMAN_WAIT_TOOLS）。

集合里现在只有一个成员：`ask_user`。

历史（为什么这个文件存在，别因为「只剩一个成员」就删掉它）：最初的缺陷是
`exit_plan_mode` ——权限门已改成无界等待后，会话事件里仍出现
`Error: tool 'exit_plan_mode' timed out after 600s`：外层 execute_tool_call 的
asyncio.wait_for 把整个工具调用连同待审批状态一起销毁，模型收到 timeout 错误后重新
调一次工具，用户「未开自动审批就一直保持未审批」的要求没有真正满足。
那个工具后来随「审批改为非阻塞会话状态」一起被删了（spec §D3：审批不再发生在任何
工具的执行期间，于是「等人的时候不能被计时器杀掉」对它根本不存在）。

`ask_user` 留下来是**它自己的**理由，与 plan 模式无关：它在执行期间等人回答
（question/request → 0.1s 轮询），自带 300s 内部上限与 abort 出口，到点返回可恢复的
「Proceed with your best judgment」；而外层默认 60s 会**先于**那个内部上限把它掐死，
模型拿到的是硬 timeout 错误而不是设计好的可恢复文案。

钉住的性质：
1. get_tool_timeout 对 HUMAN_WAIT_TOOLS（= {"ask_user"}）返回 None；表的其余项一个
   都没变——防止「把整张表都改成 None」也能过第 1 条。
2. timeout 为 None 时 execute_tool_call **不调用** asyncio.wait_for（monkeypatch
   会抛哨兵异常的 wait_for），且工具正常返回。
3. timeout 为 None 时 metadata / trace span 的 timeout_s 如实为 None——不炸、
   也不编造一个数字。
4. ask_user 正在等人回答时置 abort → 工具调用正常返回（不是永久挂着、也不是外层
   超时报错）。等待走真实 handle_ask_user 的 0.1s 轮询；测试外层用 wait_for(…, 30)
   兜底，实现改坏时变红，绝不把测试自己挂住（余量为什么是 30 而不是 3，见该处注释）。
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager

import pytest

from agents.tools import dispatcher as dispatcher_module
from agents.tools.dispatcher import HUMAN_WAIT_TOOLS, ToolDispatcher


class FakeSession:
    """只收集事件的会话替身（append 形状与 Session.append 一致）。"""

    def __init__(self) -> None:
        self.id = "s-human-wait"
        self.events: list[dict] = []
        # handle_ask_user 轮询的就是这个字典（真实 Session 上同名）
        self.question_responses: dict[str, dict] = {}

    def append(self, type_: str, data: dict | None = None) -> None:
        self.events.append({"type": type_, **(data or {})})

    def of_type(self, type_: str) -> list[dict]:
        return [e for e in self.events if e["type"] == type_]


class FakeAgent:
    def __init__(self) -> None:
        self.session = FakeSession()
        self.session_id = self.session.id
        self.current_sub_agent_id = None
        self.permission_mode = "default"


class AskUserFakeAgent(FakeAgent):
    """走到真实等人等待的最小 agent：ask_user + abort 出口。

    样板沿用本文件曾经的 PlanFakeAgent（那是本仓库「用替身走到真实等待路径」的
    现成例子）——被删的是 plan 那一半，不是这个手法。
    """

    def __init__(self) -> None:
        super().__init__()
        self.abort_flag = False

    def abort_requested(self) -> bool:
        return self.abort_flag


async def _wait_for_request(sess: FakeSession, type_: str, timeout: float = 30.0) -> dict:
    """等到请求事件落盘并返回它（真实 sleep，测试绝不挂住）。"""
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while not sess.of_type(type_):
        if loop.time() > deadline:
            raise AssertionError(f"{type_} 未落盘")
        await asyncio.sleep(0.02)
    return sess.of_type(type_)[-1]


# ── 1. 超时表：等人应答的 None，其余原样 ─────────────────────────────────


def test_timeout_table_human_wait_none_others_unchanged(monkeypatch):
    monkeypatch.delenv("MYCODE_AGENT_TOOL_TIMEOUT", raising=False)
    monkeypatch.delenv("MYCODE_SKILL_TOOL_TIMEOUT", raising=False)
    d = ToolDispatcher(agent_ref=FakeAgent())
    # 具名集合就是这一个：在自己执行期间等人应答、出口在它自己的轮询里。
    # 把 exit_plan_mode 加回来（或把 ask_user 删掉）这条就红。
    assert HUMAN_WAIT_TOOLS == frozenset({"ask_user"})
    assert d.get_tool_timeout("ask_user") is None
    # 其余一个都没变——「整张表改成 None」在这里红
    assert d.get_tool_timeout("run_shell") == 300
    assert d.get_tool_timeout("agent") == 900
    assert d.get_tool_timeout("skill") == 300
    for read_only in ("read_file", "outline_file", "grep_search", "list_files", "web_search"):
        assert d.get_tool_timeout(read_only) == 30, read_only
    assert d.get_tool_timeout("write_file") == 60  # 默认档


# ── 2. timeout=None → 不碰 asyncio.wait_for，工具正常返回 ────────────────


async def test_none_timeout_never_calls_wait_for(monkeypatch):
    class _Sentinel(Exception):
        pass

    async def boom(*args, **kwargs):
        raise _Sentinel("wait_for must not be called when timeout is None")

    monkeypatch.setattr(asyncio, "wait_for", boom)
    d = ToolDispatcher(agent_ref=FakeAgent())

    async def inner(name, inp, assistant_seq=None):
        return "ok-unbounded"

    monkeypatch.setattr(d, "_execute_tool_call_inner", inner)
    result = await d.execute_tool_call("ask_user", {})
    # 实现若仍走 wait_for：哨兵被 execute_tool_call 的 except Exception 吞成
    # error 结果，下面两条断言立刻红。
    assert result.status == "ok"
    assert result.text == "ok-unbounded"


# ── 3. timeout=None → metadata / span 的 timeout_s 如实为 None ───────────


class _RecordingSpan:
    def __init__(self, sink: dict) -> None:
        self._sink = sink

    def record_error(self, e):
        self._sink["error"] = e

    def add_metadata(self, **kw):
        self._sink.setdefault("metadata", {}).update(kw)

    def update(self, **kw):
        self._sink.setdefault("update", {}).update(kw)


@pytest.fixture
def span_sink(monkeypatch):
    sink: dict = {}

    @contextmanager
    def fake_trace_span(*args, **kwargs):
        yield _RecordingSpan(sink)

    monkeypatch.setattr(dispatcher_module, "trace_span", fake_trace_span)
    return sink


async def test_none_timeout_reports_none_not_invented_number(span_sink):
    d = ToolDispatcher(agent_ref=FakeAgent())

    async def inner_raise(name, inp, assistant_seq=None):
        raise TimeoutError("inner explicit timeout")

    async def inner_ok(name, inp, assistant_seq=None):
        return "fine"

    # 显式 TimeoutError 路径（None 时 wait_for 不会被调用，只能来自工具自身）
    d._execute_tool_call_inner = inner_raise
    result = await d.execute_tool_call("ask_user", {})
    assert result.outcome == "timeout"
    assert result.metadata["timeout_s"] is None
    assert "timed out" in result.text
    assert "Nones" not in result.text  # 不许把 None 拼进「after Nones」这种文案
    assert span_sink["metadata"]["timeout_s"] is None  # trace 里也是 None，不炸

    # 成功路径
    d._execute_tool_call_inner = inner_ok
    result2 = await d.execute_tool_call("ask_user", {})
    assert result2.status == "ok"
    assert result2.metadata["timeout_s"] is None


# ── 4. abort 能把无界等待放出来（工具级全链路） ──────────────────────────


async def test_abort_releases_unbounded_ask_user_wait():
    agent = AskUserFakeAgent()
    d = ToolDispatcher(agent_ref=agent)
    task = asyncio.create_task(
        d.execute_tool_call("ask_user", {"question": "继续吗？"}))
    req = await _wait_for_request(agent.session, "question/request")
    assert req["question"] == "继续吗？"
    agent.abort_flag = True
    # 兜底 wait_for：无界超时下没有任何外层计时器，实现改坏（比如重新引入
    # wait_for、或 abort 出口失效）时这里变红，而不是挂死测试会话。
    # 余量刻意给到 30s 而不是 3s：轮询节拍是 0.1s，正常情况下毫秒级就返回，
    # 但全套一千多个测试并发压上来时事件循环会被拖慢——3s 的余量在满载机器上
    # 真的会假失败（本文件第一次提交时就中过一次：单跑绿、全套红）。
    # 放宽的代价只是「实现真坏时要等 30s 才看到红」，比一条间歇性卡门禁的测试便宜得多。
    result = await asyncio.wait_for(task, 30)
    assert result.status == "ok"
    assert result.metadata["timeout_s"] is None
    assert "timed out" not in result.text  # 不是被外层超时掐死的
    assert "cancelled by abort" in result.text
