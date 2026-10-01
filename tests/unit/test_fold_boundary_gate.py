"""折叠任务边界门：有 in_progress 任务时不折叠，硬顶除外。"""
from __future__ import annotations

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


# ---- 穿线：defer_fold 必须真的从 check_and_compact 走到 run_pipeline ----
#
# 门上每一段都带默认值（defer_fold: bool = False），所以「接了但没人传 True」
# 在单测里长得和「接对了」一模一样——只有驱动整条链才看得见。链路：
#   model_caller → Agent.check_and_compact (agents/agent.py)
#                → ContextManager._check_and_compact (agents/core/context.py)
#                → ContextCompressor.run_pipeline → _should_compress
# 少穿任何一跳，闸门静默永不生效，而折叠照旧落在任务中途。


def _make_recording_agent(ws, **kwargs):
    from agents.agent import Agent

    agent = Agent(model="stub", api_base="http://localhost:1/v1", api_key="k",
                  workspace=ws, **kwargs)
    seen: dict = {}

    async def fake_run_pipeline(session, current_token_count, last_api_call_time,
                                side_query, session_id, defer_fold=False):
        seen["defer_fold"] = defer_fold
        seen["session_id"] = session_id
        return False                      # 不折叠 → 不触发 reset_context_token_estimate

    agent._compressor = SimpleNamespace(run_pipeline=fake_run_pipeline)
    agent._build_side_query = lambda max_tokens=2000: None
    return agent, seen


async def test_check_and_compact_passes_defer_fold_true_mid_task(ws):
    agent, seen = _make_recording_agent(ws)
    item = add_task(agent.session.id, "A")
    update_task(agent.session.id, item.id, status="in_progress", current_seq=5)

    await agent.check_and_compact()

    assert seen["defer_fold"] is True
    assert seen["session_id"] == agent.session.id


async def test_check_and_compact_passes_defer_fold_false_when_no_task_running(ws):
    """没有 in_progress 任务时照常折叠——门只推迟，不改变无任务时的行为。"""
    agent, seen = _make_recording_agent(ws)
    add_task(agent.session.id, "A")            # pending，不上膛

    await agent.check_and_compact()

    assert seen["defer_fold"] is False
