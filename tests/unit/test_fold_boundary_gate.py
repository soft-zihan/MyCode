"""折叠任务边界门：有 in_progress 任务时不折叠，硬顶除外。"""
from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from agents.core.context_compressor import (
    FOLD_DEFER_CEILING_MAX,
    FOLD_DEFER_CEILING_OFFSET,
    ContextCompressor,
)
from agents.core.workspace import reset_workspace, set_workspace
from agents.tools.task_store import add_task, update_task


@pytest.fixture
def ws(tmp_path):
    token = set_workspace(tmp_path)
    yield tmp_path
    reset_workspace(token)


def _comp(threshold=0.80, idle=300):
    return ContextCompressor(effective_window=100_000,
                             tool_fold_threshold=threshold,
                             idle_timeout_seconds=idle)


def test_hard_ceiling_is_threshold_plus_offset_capped():
    assert min(0.80 + FOLD_DEFER_CEILING_OFFSET, FOLD_DEFER_CEILING_MAX) == 0.90
    # 阈值很高时硬顶不超过 0.95，绝不贴着窗口边缘
    assert min(0.92 + FOLD_DEFER_CEILING_OFFSET, FOLD_DEFER_CEILING_MAX) == 0.95


@pytest.mark.parametrize("threshold,just_under,just_over", [
    # 硬顶 = min(0.92 + 0.10, 0.95) = 0.95，MAX 这一半**真的生效**。
    # 上面那个测试在字面量上重算 min()，钉住的只是两个常量的值；而其余
    # _should_compress 测试一律用 threshold=0.80，那里 MAX 是惰性的
    # （0.80 + 0.10 = 0.90 < 0.95）。于是删掉生产表达式里的
    # `, FOLD_DEFER_CEILING_MAX)` → 硬顶变成 1.02 → just_over 那条断言反向
    # （0.96 > 1.02 = False），而全套其余测试照旧全绿。
    (0.92, 0.93, 0.96),
])
def test_hard_ceiling_cap_bites_in_production_code(threshold, just_under, just_over):
    """cap 那一半必须经生产代码被行使：绝不让上下文贴到窗口边缘。"""
    c = _comp(threshold=threshold)
    assert c._should_compress(just_under, 0.0, defer_fold=True) is False
    assert c._should_compress(just_over, 0.0, defer_fold=True) is True


def test_no_defer_folds_at_threshold():
    c = _comp()
    assert c._should_compress(0.85, 0.0) is True
    assert c._should_compress(0.75, 0.0) is False


def test_defer_blocks_fold_between_threshold_and_ceiling():
    c = _comp()
    assert c._should_compress(0.85, 0.0, defer_fold=True) is False


def test_defer_still_folds_above_hard_ceiling():
    """安全阀：真的快撑爆窗口时，宁可破坏当前任务也不能失败。"""
    c = _comp()
    assert c._should_compress(0.95, 0.0, defer_fold=True) is True


def test_defer_at_exactly_ceiling_does_not_fold():
    c = _comp()
    assert c._should_compress(0.90, 0.0, defer_fold=True) is False


def test_defer_also_suppresses_the_idle_trigger():
    """空闲超时同样不得在任务中途折叠——那正是本门要防的事。"""
    c = _comp(idle=300)
    assert c._should_compress(0.10, 999.0) is True                  # 不推迟：空闲触发
    assert c._should_compress(0.10, 999.0, defer_fold=True) is False  # 推迟：不触发


def test_defer_default_is_false_so_existing_callers_unchanged():
    c = _comp()
    assert c._should_compress(0.85, 0.0) is c._should_compress(0.85, 0.0, False)


def test_has_in_progress_task_true(ws):
    from agents.agent import Agent
    item = add_task("s1", "A")
    update_task("s1", item.id, status="in_progress", current_seq=5)
    agent = Agent(model="stub", api_base="http://localhost:1/v1", api_key="k",
                  session_id="s1")
    assert agent._has_in_progress_task() is True


def test_has_in_progress_task_false_when_all_pending(ws):
    from agents.agent import Agent
    add_task("s1", "A")
    agent = Agent(model="stub", api_base="http://localhost:1/v1", api_key="k",
                  session_id="s1")
    assert agent._has_in_progress_task() is False


def test_has_in_progress_task_false_when_store_unreadable(ws, monkeypatch):
    """读不出来必须返回 False —— 绝不能因为清单读不出来而不压缩，那会撑爆窗口。"""
    from agents.agent import Agent
    agent = Agent(model="stub", api_base="http://localhost:1/v1", api_key="k",
                  session_id="s1")
    import agents.tools.task_store as ts
    monkeypatch.setattr(ts, "list_tasks", lambda _sid: (_ for _ in ()).throw(OSError("boom")))
    assert agent._has_in_progress_task() is False


def test_has_in_progress_task_false_when_no_list(ws):
    from agents.agent import Agent
    agent = Agent(model="stub", api_base="http://localhost:1/v1", api_key="k",
                  session_id="s1")
    assert agent._has_in_progress_task() is False


# ---- 穿线：defer_fold 必须真的从 check_and_compact 走到 _should_compress ----
#
# 门上每一段都带默认值，所以「接了但没人传」在单测里长得和「接对了」一模一样——
# 只有驱动链路才看得见。完整链路：
#   model_caller → Agent.check_and_compact            (agents/agent.py)
#                → ContextManager._check_and_compact   (agents/core/context.py)
#                → ContextCompressor.run_pipeline      (agents/core/context_compressor.py)
#                → ContextCompressor._should_compress
#
# 本文件对这条链的覆盖是**两段拼接**，不是一个整体，说清楚各自钉住哪几跳：
#   · 下面这两个 check_and_compact 测试把 agent._compressor 换成 SimpleNamespace，
#     所以钉住的是**前三跳**（Agent.check_and_compact → context.py 的纯中转 →
#     run_pipeline 这个调用点本身）。run_pipeline 的实现及其之后一律不在网内。
#   · test_run_pipeline_forwards_defer_fold_into_should_compress 反过来：保留
#     **真** ContextCompressor、只替换 _should_compress，钉住的是**最后一跳**。
# 两段各自都少不得：删掉 context.py 的透传只有前者会红；删掉 run_pipeline 里传给
# _should_compress 的那个实参只有后者会红（此前后者不存在，所以那一跳无网）。


def _make_recording_agent(ws, **kwargs):
    from agents.agent import Agent

    agent = Agent(model="stub", api_base="http://localhost:1/v1", api_key="k",
                  workspace=ws, **kwargs)
    seen: dict = {}

    async def fake_run_pipeline(session, current_token_count, last_api_call_time,
                                side_query, session_id, defer_fold=None):
        seen["defer_fold"] = defer_fold
        seen["session_id"] = session_id
        return False                      # 不折叠 → 不触发 reset_context_token_estimate

    agent._compressor = SimpleNamespace(run_pipeline=fake_run_pipeline)
    agent._build_side_query = lambda max_tokens=2000: None
    return agent, seen


async def test_check_and_compact_hands_over_an_unread_probe_mid_task(ws):
    """前三跳：交出去的是**零参 callable**（探针本身），不是已经读好的 bool。

    交值就意味着每次模型调用都读一次任务清单——哪怕利用率是 1%、这一轮根本不做
    折叠决定（热路径上推式披露与常驻摘要已各读一次，这会是第三次）。所以这里
    同时钉两件事：交接路径上一次都没读盘，以及压缩器真去问的时候答案是 True。
    """
    agent, seen = _make_recording_agent(ws)
    probes: list[int] = []
    real_probe = agent._has_in_progress_task
    agent._has_in_progress_task = lambda: (probes.append(1), real_probe())[1]
    item = add_task(agent.session.id, "A")
    update_task(agent.session.id, item.id, status="in_progress", current_seq=5)

    await agent.check_and_compact()

    assert probes == []                       # 交接时不读盘
    assert callable(seen["defer_fold"])       # 交出去的是探针本身
    assert seen["defer_fold"]() is True       # 压缩器真要时才读，且答案正确
    assert probes == [1]
    assert seen["session_id"] == agent.session.id


async def test_check_and_compact_hands_over_a_probe_reading_false_when_idle(ws):
    """没有 in_progress 任务时照常折叠——门只推迟，不改变无任务时的行为。"""
    agent, seen = _make_recording_agent(ws)
    add_task(agent.session.id, "A")            # pending，不上膛

    await agent.check_and_compact()

    assert callable(seen["defer_fold"])
    assert seen["defer_fold"]() is False


# ---- 最后一跳：run_pipeline → _should_compress ----


async def test_run_pipeline_forwards_defer_fold_into_should_compress():
    """保留真 ContextCompressor，只替换 _should_compress，钉住最后一跳。

    变异证据：删掉 run_pipeline 里传给 _should_compress 的第三个实参（默认值
    False 静默接管），全套其余测试照旧全绿，而闸门永不生效——上面两个穿线测试
    看不见这一跳（它们的 run_pipeline 是替身），7 个 _should_compress 测试也
    看不见（它们直接调它）。
    """
    c = _comp()
    seen: dict = {}

    def recording_should_compress(utilization, idle_seconds, defer_fold=False):
        seen["defer_fold"] = defer_fold
        return False                    # 不折叠 → run_pipeline 提前返回，不碰 session

    c._should_compress = recording_should_compress
    await c.run_pipeline(SimpleNamespace(events=[]), 85_000, time.time(), None,
                         "fold-gate-s", defer_fold=True)

    assert seen["defer_fold"] is True


async def test_run_pipeline_probes_the_task_store_only_near_a_trigger():
    """探针是零参 callable，且**只在逼近触发点时**才被调用。

    生产路径上 Agent.check_and_compact 每次模型调用都跑，而它此前 eagerly 调
    _has_in_progress_task() 再把 bool 传下来 —— 于是每次模型调用都多一次任务清单
    磁盘读，哪怕利用率是 1%、这一轮根本不做折叠决定。
    同时钉住：交给 _should_compress 的是**已解析的 bool**（把 callable 直接透传
    会让 trace metadata 与那行 print 里出现一个 <bound method ...>，事后什么也
    看不出来）。
    """
    c = _comp()
    calls: list[int] = []
    seen: list = []
    real_should_compress = c._should_compress

    def probe() -> bool:
        calls.append(1)
        return True

    def spy(utilization, idle_seconds, defer_fold=False):
        seen.append(defer_fold)
        return real_should_compress(utilization, idle_seconds, defer_fold)

    c._should_compress = spy
    session = SimpleNamespace(events=[])

    # 1% 利用率、空闲 0s：离任何触发点都远 → 不读盘，也不折叠
    assert await c.run_pipeline(session, 1_000, time.time(), None, "s",
                                defer_fold=probe) is False
    assert calls == []
    assert seen == [False]

    # 85% 利用率：越过阈值 → 值得问门；门说 True → 硬顶 0.90 挡住，仍不折叠
    assert await c.run_pipeline(session, 85_000, time.time(), None, "s",
                                defer_fold=probe) is False
    assert calls == [1]
    assert seen == [False, True]

    # 空闲超时同样算「逼近触发点」——空闲折叠恰恰最容易落在任务中途
    calls.clear()
    seen.clear()
    assert await c.run_pipeline(session, 1_000, time.time() - 999, None, "s",
                                defer_fold=probe) is False
    assert calls == [1]
    assert seen == [True]
