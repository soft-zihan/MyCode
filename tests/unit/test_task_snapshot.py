"""每请求一次的任务清单快照（Plan 2 裁定的兑现，Plan 3b Task B1）。

Plan 2 裁定「不加 mtime/lru 缓存」，同时明写「Plan 3 引入第二个写入方时，每请求
只读一次、把快照传给三个消费者」。第二个写入方（UI 写端点）现在到了。

三个消费者都在 `ModelCaller._attempt` 的调用链上：
  1. 折叠门探针 `Agent._has_in_progress_task`（经 check_and_compact → run_pipeline）
  2. 推式披露 `ensure_focus_detail_visible`
  3. 常驻摘要 S（`build_tail_system_messages`，在 _assemble_request 里）

没有快照时它们是三次独立的读，于是「一次写入落在它们之间」是可读的：折叠门可能在
任务中途决定折叠，而模型眼前的 S 说的是另一回事。快照是**请求作用域**的，没有失效
逻辑，所以不违反「不加缓存」（那条裁定反对的是跨请求缓存）。

必须保住的三条既有裁定：
  · `defer_fold` 仍是零参 callable，且只在逼近触发点时才被调用（不退回急切求值）；
  · `_has_in_progress_task` 的失败方向仍是 False（读不出来 → 照常折叠）；
  · `context_compressor` 不 import task_store，`context.py` 仍是纯中转。
"""
from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from agents.core.context_compressor import ContextCompressor
from agents.core.model_caller import ModelCaller
from agents.core.prompt_runtime import build_tail_system_messages
from agents.core.session import Session
from agents.core.workspace import reset_workspace, set_workspace
from agents.tools import task_store
from agents.tools.task_disclosure import ensure_focus_detail_visible
from agents.tools.task_store import add_task, list_tasks, update_task

SID = "s-snapshot"
EXTRA = "UI 在两次读之间写入的任务"


@pytest.fixture
def ws(tmp_path):
    token = set_workspace(tmp_path)
    yield tmp_path
    reset_workspace(token)


def _agent(session_id: str = SID, **kwargs):
    """真 Agent（is_sub_agent → Session(origin="sub_agent")），离线可构造。"""
    from agents.agent import Agent

    return Agent(model="stub", api_base="http://localhost:1/v1", api_key="k",
                 session_id=session_id, is_sub_agent=True, **kwargs)


def _tail_agent(session_id: str = SID):
    """build_tail_system_messages 读取的全部属性（同 test_task_list_tail 的桩）。"""
    return SimpleNamespace(
        _custom_system_prompt=None,
        permission_mode="default",
        _plan_mode_manager=None,
        estimated_context_tokens=1000,
        effective_window=100000,
        _fold_last_time=0.0,
        _tool_error_streak=0,
        _same_tool_repeat_count=0,
        session=SimpleNamespace(id=session_id),
    )


def _short_circuit_model_call(monkeypatch, agent) -> None:
    """让 ModelCaller.call 走完 check_and_compact → 披露 → _assemble_request，
    然后在真正发请求处短路。

    - with_retry 换成单发：重试会把 _attempt 跑多次，读数就不再是「每请求」的读数。
    - get_active_tool_definitions 换成 []：工具定义与快照无关，且它会去读 MCP/技能。
    - create 抛哨兵：_consume_stream 根本不需要伪造异步 chunk 流。
    """
    async def _single_shot(fn, *args, **kwargs):
        return await fn()

    monkeypatch.setattr("agents.core.model_caller.with_retry", _single_shot)
    monkeypatch.setattr(
        "agents.core.model_caller.get_active_tool_definitions", lambda tools: [])

    async def _boom(**kwargs):
        raise RuntimeError("sentinel: 短路在发请求处")

    agent._openai_client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=_boom)))


def _count_store_reads(monkeypatch, *, on_first_read=None) -> list[str]:
    """数 store 读次数。打在 load_tasks 上——它是所有读取路径的唯一漏斗。

    三个消费者各自 `from task_store import list_tasks` 绑了本地名，打 list_tasks
    只能拦住 task_store 自己命名空间里的那一次；load_tasks 是它们共同调用的模块级
    名字，一处就够。写入路径（add_task / update_task / mark_detail_disclosed）也经
    load_tasks，所以 on_first_read 里做写入时用 `counting["on"]` 关掉计数——那一次
    是写，不是消费者的读。
    """
    real_load = task_store.load_tasks
    calls: list[str] = []
    counting = {"on": True}

    def counting_load(session_id):
        result = real_load(session_id)
        if counting["on"]:
            calls.append(session_id)
            if on_first_read is not None and len(calls) == 1:
                counting["on"] = False
                try:
                    on_first_read(session_id)
                finally:
                    counting["on"] = True
        return result

    monkeypatch.setattr(task_store, "load_tasks", counting_load)
    return calls


def _instrument(agent, monkeypatch):
    """记录折叠门探针的调用次数与 S 的内容（两者都保持真实现的行为）。"""
    probe_calls: list[int] = []
    real_probe = agent._has_in_progress_task

    def recording_probe(*args, **kwargs):
        probe_calls.append(1)
        return real_probe(*args, **kwargs)

    agent._has_in_progress_task = recording_probe

    tails_seen: list[list[str]] = []
    real_tails = agent.build_tail_system_messages

    def recording_tails(*args, **kwargs):
        result = real_tails(*args, **kwargs)
        tails_seen.append(result)
        return result

    agent.build_tail_system_messages = recording_tails
    return probe_calls, tails_seen


def _near_trigger(agent, utilization_tokens: int) -> None:
    """把利用率摆到「逼近触发点但被任务边界门挡住」的位置。

    0.85 越过阈值 0.80 → _resolve_defer_fold 会去问探针；探针答 True（有
    in_progress 任务）→ _should_compress 只认硬顶 0.90 → 不折叠。于是探针被真调用
    了，而管线不会去碰 LLM。
    """
    agent._compressor = ContextCompressor(effective_window=100_000,
                                          tool_fold_threshold=0.80,
                                          idle_timeout_seconds=300)
    agent.last_total_token_count = utilization_tokens
    agent.last_usage_seq = 10 ** 6          # 超过所有事件 → 增量估算为 0
    agent.last_api_call_time = time.time()  # idle ≈ 0，不走空闲触发


def _in_progress_task(session_id: str = SID):
    item = add_task(session_id, "A")
    update_task(session_id, item.id, status="in_progress")
    return item


# ─────────────────── 一次读服务三个消费者 ───────────────────


async def test_one_store_read_serves_all_three_consumers(ws, monkeypatch):
    """近触发点（探针真的被问）+ 一次落在读之间的 UI 写入。

    钉两件事：
      · store 只被读一次（此前是三次：探针 / 披露 / S 各一次）；
      · 三个消费者看到的是**同一份**清单——第一次读之后写进去的那条不出现在 S 里。
        这正是快照要防的事：写入落在消费者之间时，折叠门与 S 会各说各话。
    """
    _in_progress_task()
    agent = _agent()
    _near_trigger(agent, 85_000)
    _short_circuit_model_call(monkeypatch, agent)
    probe_calls, tails_seen = _instrument(agent, monkeypatch)
    calls = _count_store_reads(
        monkeypatch, on_first_read=lambda sid: add_task(sid, EXTRA))

    with pytest.raises(RuntimeError, match="sentinel"):
        await ModelCaller(agent).call()

    assert calls == [SID], f"每请求应当只有一次 store 读，实际 {len(calls)} 次"
    assert probe_calls == [1]                      # 折叠门这一路确实参与了本次请求
    assert [t.content for t in list_tasks(SID)] == ["A", EXTRA]   # 写入真的落盘了

    block = next(t for t in tails_seen[0] if "Task List" in t)
    assert "#1 A" in block                         # S 渲染的是快照
    assert EXTRA not in block                      # 而不是读之后那次写入


async def test_probe_is_not_asked_when_far_from_any_trigger(ws, monkeypatch):
    """defer_fold 的惰性没有被快照破坏。

    利用率 1% 时 _resolve_defer_fold 压根不问探针，所以折叠门这一路一次都不参与；
    整个请求的 store 读仍然只有快照那一次（此前是披露 + S 两次）。
    """
    add_task(SID, "A")                             # pending，门本来也不会开
    agent = _agent()
    _near_trigger(agent, 1_000)                    # 1% 利用率，离三条触发线都远
    _short_circuit_model_call(monkeypatch, agent)
    probe_calls, tails_seen = _instrument(agent, monkeypatch)
    calls = _count_store_reads(monkeypatch)

    with pytest.raises(RuntimeError, match="sentinel"):
        await ModelCaller(agent).call()

    assert probe_calls == []                       # 离触发点远 → 不问探针
    assert calls == [SID]                          # 且没有别的地方替它读
    assert any("Task List" in t for t in tails_seen[0])


async def test_probe_handed_to_the_compressor_closes_over_the_snapshot(ws, monkeypatch):
    """交下去的仍是零参 callable，而它读的是快照、不是盘。

    与 test_fold_boundary_gate 的两个交接测试同一条性质（不退回急切求值的 bool），
    加上新的一半：调用探针不再产生 store 读。
    """
    _in_progress_task()
    agent = _agent()
    seen: dict = {}

    async def fake_run_pipeline(session, tokens, last_api_call_time, side_query,
                                session_id, defer_fold=None):
        seen["defer_fold"] = defer_fold
        return False

    agent._compressor = SimpleNamespace(run_pipeline=fake_run_pipeline)
    agent._build_side_query = lambda max_tokens=2000: None
    snapshot = list_tasks(SID)                     # 快照在计数之前就已读好
    calls = _count_store_reads(monkeypatch)

    await agent.check_and_compact(snapshot)

    assert callable(seen["defer_fold"])            # 不是已经读好的 bool
    assert calls == []                             # 交接时不读盘
    assert seen["defer_fold"]() is True            # 压缩器真要时才问，答案正确
    assert calls == []                             # 而它读的是闭包里的快照


async def test_check_and_compact_without_a_snapshot_still_probes_the_store(ws, monkeypatch):
    """没有快照的调用方（不经 _attempt 的直接调用，例如本测试）行为不变。"""
    _in_progress_task()
    agent = _agent()
    seen: dict = {}

    async def fake_run_pipeline(session, tokens, last_api_call_time, side_query,
                                session_id, defer_fold=None):
        seen["defer_fold"] = defer_fold
        return False

    agent._compressor = SimpleNamespace(run_pipeline=fake_run_pipeline)
    agent._build_side_query = lambda max_tokens=2000: None
    calls = _count_store_reads(monkeypatch)

    await agent.check_and_compact()

    assert callable(seen["defer_fold"])
    assert calls == []
    assert seen["defer_fold"]() is True
    assert calls == [SID]                          # 这一路仍然自己读盘


# ─────────────────── 失败方向与无快照回退 ───────────────────


def test_probe_failure_direction_stays_false(ws, monkeypatch):
    """读不出来一律 False（照常折叠）——快照参数不得改变这条裁定。"""
    agent = _agent()

    def _boom(_sid):
        raise OSError("boom")

    monkeypatch.setattr(task_store, "list_tasks", _boom)
    assert agent._has_in_progress_task() is False
    assert agent._has_in_progress_task(None) is False


def test_probe_trusts_an_empty_snapshot_without_reading(ws, monkeypatch):
    """`[]` 是「读到了，确实没任务」，不是「没有快照」——不得再去读一次。

    用 falsy 判断（`if tasks:`）会让空清单退化成一次多余的磁盘读，并且把
    「store 读不出来」与「没有任务」两种状态混在一起。
    """
    agent = _agent()
    calls = _count_store_reads(monkeypatch)
    assert agent._has_in_progress_task([]) is False
    assert calls == []


def test_consumers_still_work_without_a_snapshot(ws):
    """三个消费者都能被直接调用（既有测试就是这么用的）：参数有回退默认值。"""
    add_task(SID, "A", detail="方案A")

    session = Session(SID, origin="sub_agent")
    assert ensure_focus_detail_visible(session) is True
    assert any("方案A" in (e.get("content") or "")
               for e in session.events if e.get("type") == "memory_injection")

    assert any("Task List" in t for t in build_tail_system_messages(_tail_agent()))

    agent = _agent()
    assert agent._has_in_progress_task() is False      # pending，不是 in_progress


def test_snapshot_loader_returns_none_when_the_store_is_unreadable(ws, monkeypatch):
    """None 表示「这次没有快照」，让各消费者落回自己的读与自己的失败方向。"""
    def _boom(_sid):
        raise OSError("boom")

    monkeypatch.setattr(task_store, "list_tasks", _boom)
    assert task_store.try_list_tasks(SID) is None


def test_snapshot_loader_returns_the_list(ws):
    add_task(SID, "A")
    snapshot = task_store.try_list_tasks(SID)
    assert snapshot is not None
    assert [t.content for t in snapshot] == ["A"]


def test_context_compressor_still_does_not_import_task_store():
    """裁定：失败方向的裁定权在 Agent，压缩器只认「一个返回 bool 的零参 callable」。

    快照把读取点搬到了 model_caller，最容易顺手做的错事就是让压缩器自己去读 store
    （或 import task_store 来判 in_progress）。这条把那道门钉住。用 AST 而不是搜文本
    ——本模块的注释里就写着「不 import task_store」，文本搜索会自相矛盾。
    """
    import ast

    import agents.core.context_compressor as cc

    tree = ast.parse(open(cc.__file__, encoding="utf-8").read())
    imported: set[str] = set()
    for node in ast.walk(tree):            # walk：函数级延迟 import 也算
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
    assert not any("task_store" in name for name in imported), sorted(imported)
