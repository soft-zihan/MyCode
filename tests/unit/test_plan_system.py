"""Plan 系统单元测试：双轨分层、任务状态机、格式契约、策略插件引擎。"""

from __future__ import annotations

from pathlib import Path

import pytest

from agents.plan import plan_manager as pm
from agents.plan.plan_models import PlanGranularity, PlanStatus
from agents.plan.plan_mode import PlanModeManager
from agents.plan.strategy_loader import (
    DEFAULT_STRATEGIES,
    load_strategy,
    strategy_config_from_app_config,
)
from agents.plan.plan_executor import PlanExecutor


STRUCTURED_TASKS = """<!-- TASKS START -->
### Task 1: 实现 foo
- **文件**: `src/foo.py`
- **验收**: `pytest tests/test_foo.py`
- **状态**: [ ] pending

### Task 2: 实现 bar
- **文件**: `src/bar.py`
- **验收**: `pytest tests/test_bar.py`
- **状态**: [ ] pending
<!-- TASKS END -->
"""

SIMPLE_TASKS = """## 任务清单

- [ ] 1. 第一个任务
- [x] 2. 已完成任务
- [~] 3. 进行中任务
- [!] 4. 失败任务
- [-] 5. 跳过任务
"""


@pytest.fixture()
def plans_ws():
    """当前 workspace（isolated_home 已把 cwd 指向 tmp workdir）。"""
    return Path.cwd()


def _make_standard_plan(slug: str, tasks_content: str = STRUCTURED_TASKS) -> None:
    pm.create_plan(slug=slug, granularity=PlanGranularity.STANDARD)
    pm.add_artifact(slug, "tasks.md", tasks_content)


# ────────────────────────── 解析与计数 ──────────────────────────

class TestParsing:

    def test_count_structured_tasks(self):
        assert pm.count_tasks(STRUCTURED_TASKS) == 2

    def test_count_simple_tasks(self):
        assert pm.count_tasks(SIMPLE_TASKS) == 5

    def test_parse_structured_status(self):
        _make_standard_plan("parse-struct")
        tasks = pm.get_tasks("parse-struct")
        assert [t.id for t in tasks] == [1, 2]
        assert all(t.status == "pending" for t in tasks)
        assert tasks[0].file == "src/foo.py"

    def test_parse_simple_all_markers(self):
        _make_standard_plan("parse-simple", SIMPLE_TASKS)
        statuses = {t.id: t.status for t in pm.get_tasks("parse-simple")}
        assert statuses == {1: "pending", 2: "done", 3: "in-progress", 4: "failed", 5: "skipped"}


# ────────────────────────── 状态机 ──────────────────────────

class TestStateMachine:

    def test_done_flow_and_auto_complete(self):
        _make_standard_plan("flow-done")
        assert pm.mark_task_in_progress("flow-done", 1)
        assert pm.mark_task_done("flow-done", 1, commit="c1",
                                 verification={"command": "pytest", "exit_code": 0})
        assert pm.get_tasks("flow-done")[0].status == "done"
        assert pm.mark_task_in_progress("flow-done", 2)
        assert pm.mark_task_done("flow-done", 2, commit="c2",
                                 verification={"command": "pytest", "exit_code": 0})
        # 全部 done → 自动 completed
        assert pm.get_plan("flow-done").status == PlanStatus.COMPLETED

    def test_failed_then_skip_then_redo(self):
        _make_standard_plan("flow-fsr", "### Task 1: A\n- **状态**: [ ] pending\n\n### Task 2: B\n- **状态**: [ ] pending\n")
        pm.mark_task_in_progress("flow-fsr", 1)
        assert pm.mark_task_failed("flow-fsr", 1, reason="boom")
        assert pm.get_tasks("flow-fsr")[0].status == "failed"
        assert pm.skip_task("flow-fsr", 1)
        assert pm.get_tasks("flow-fsr")[0].status == "skipped"
        assert pm.redo_task("flow-fsr", 1)
        assert pm.get_tasks("flow-fsr")[0].status == "pending"

    def test_skip_redo_preconditions(self):
        _make_standard_plan("flow-pre", "### Task 1: A\n- **状态**: [ ] pending\n")
        assert not pm.redo_task("flow-pre", 1)  # pending 不可 redo
        assert pm.skip_task("flow-pre", 1)
        assert not pm.skip_task("flow-pre", 1)  # skipped 不可再 skip
        assert pm.redo_task("flow-pre", 1)

    def test_update_status_does_not_leak_across_blocks(self):
        _make_standard_plan("flow-block")
        pm.mark_task_in_progress("flow-block", 1)
        content = pm.read_artifact("flow-block", "tasks.md")
        assert "[~] in-progress" in content
        # Task 2 不受影响
        assert pm.get_tasks("flow-block")[1].status == "pending"

    def test_simple_task_status_update_in_file(self):
        _make_standard_plan("flow-simple", SIMPLE_TASKS)
        assert pm.mark_task_in_progress("flow-simple", 1)
        content = pm.read_artifact("flow-simple", "tasks.md")
        assert "- [~] 1. 第一个任务" in content


# ────────────────────────── Ledger ──────────────────────────

class TestLedger:

    def test_ledger_records_lifecycle(self):
        _make_standard_plan("ledger-1", "### Task 1: A\n- **状态**: [ ] pending\n")
        pm.mark_task_in_progress("ledger-1", 1)
        pm.mark_task_done("ledger-1", 1, commit="c1",
                          verification={"command": "echo", "exit_code": 0})
        pm.redo_task("ledger-1", 1)
        pm.skip_task("ledger-1", 1)
        # ledger 记录终态事件（in-progress 过渡不落账）
        events = [e.get("status") for e in pm.read_ledger("ledger-1")]
        for expected in ("done", "redo", "skipped"):
            assert expected in events, events


# ────────────────────────── 双轨校验 ──────────────────────────

class TestDualTrack:

    def _mgr(self, session_id: str) -> PlanModeManager:
        mgr = PlanModeManager(workspace=Path.cwd(), session_id=session_id)
        mgr.plan_dir = mgr.generate_plan_dir()
        return mgr

    def test_read_draft_granularity_minimal(self):
        mgr = self._mgr("s-min")
        (mgr.plan_dir / "plan.md").write_text("# P\n", encoding="utf-8")
        assert mgr.read_draft_artifacts()["granularity"] == "minimal"

    def test_read_draft_granularity_standard(self):
        mgr = self._mgr("s-std")
        (mgr.plan_dir / "plan.md").write_text("# P\n", encoding="utf-8")
        (mgr.plan_dir / "spec.md").write_text("# S\n", encoding="utf-8")
        assert mgr.read_draft_artifacts()["granularity"] == "standard"

    def test_validate_minimal_pass_and_fail(self):
        mgr = self._mgr("s-vmin")
        (mgr.plan_dir / "plan.md").write_text(
            "# Plan\n\n## 任务清单\n\n- [ ] 1. 做点什么\n\n## 验收\n\n跑测试\n", encoding="utf-8")
        result = mgr.validate_plan_artifacts()
        assert result["valid"], result["errors"]
        assert result["granularity"] == "minimal"

        (mgr.plan_dir / "plan.md").write_text("# Plan\n\n没有任务\n", encoding="utf-8")
        result = mgr.validate_plan_artifacts()
        assert not result["valid"]

    def test_validate_standard_pass_and_fail(self):
        mgr = self._mgr("s-vstd")
        (mgr.plan_dir / "spec.md").write_text(
            "# Spec\n\n## 验收标准\n\n- [ ] 接口返回 200\n", encoding="utf-8")
        (mgr.plan_dir / "tasks.md").write_text(STRUCTURED_TASKS, encoding="utf-8")
        result = mgr.validate_plan_artifacts()
        assert result["valid"], result["errors"]
        assert result["granularity"] == "standard"

        (mgr.plan_dir / "spec.md").write_text("# Spec\n\nonly requirements here\n", encoding="utf-8")
        result = mgr.validate_plan_artifacts()
        assert not result["valid"]
        assert any("验收标准" in e for e in result["errors"])

    def test_checkbox_tasks_to_structured(self):
        out = PlanModeManager.checkbox_tasks_to_structured(SIMPLE_TASKS)
        assert "### Task 1: 第一个任务" in out
        assert "[~] in-progress" in out
        assert "[x] done" in out
        assert pm.count_tasks(out) == 5


# ────────────────────────── 策略加载与插件引擎 ──────────────────────────

class TestStrategies:

    def test_load_builtin_default(self):
        content, source, resolved = load_strategy(
            "execute", "direct", Path.cwd(), Path.cwd() / ".mycode/plans/x")
        assert source == "builtin" and resolved == "direct"
        assert "plan_task_start" in content

    def test_project_override_wins(self):
        custom_dir = Path.cwd() / ".mycode" / "plan-strategies" / "execute"
        custom_dir.mkdir(parents=True, exist_ok=True)
        (custom_dir / "direct.md").write_text("PROJECT CUSTOM", encoding="utf-8")
        content, source, resolved = load_strategy(
            "execute", "direct", Path.cwd(), Path.cwd() / ".mycode/plans/x")
        assert source == "project" and content.strip() == "PROJECT CUSTOM"

    def test_missing_strategy_falls_back(self):
        content, source, resolved = load_strategy(
            "execute", "no-such-strategy", Path.cwd(), Path.cwd() / ".mycode/plans/x")
        assert resolved != "no-such-strategy"
        assert source == "fallback"

    def test_app_config_key_translation(self):
        cfg = strategy_config_from_app_config()
        assert set(cfg.keys()) == set(DEFAULT_STRATEGIES.keys())

    def test_executor_default_strategies(self):
        _make_standard_plan("exec-default")
        ex = PlanExecutor(slug="exec-default", workspace=Path.cwd())
        guide = ex.build_execute_instructions()
        assert "plan_task_start" in guide and "plan_complete" in guide
        assert ex.build_review_guidance() == ""   # review=none
        assert ex.build_converge_guidance() == ""  # converge=none

    def test_executor_explicit_strategies(self):
        _make_standard_plan("exec-explicit")
        ex = PlanExecutor(slug="exec-explicit", workspace=Path.cwd(),
                          strategy_config={"execute": "tdd", "review": "dual-axis",
                                           "converge": "gap-analysis"})
        assert "Red" in ex.build_execute_instructions()
        assert ex.build_review_guidance()
        converge = ex.build_converge_guidance()
        assert converge and "{plan_dir}" not in converge

    def test_executor_slug_placeholder(self):
        _make_standard_plan("exec-slug")
        ex = PlanExecutor(slug="exec-slug", workspace=Path.cwd(),
                          strategy_config={"execute": "subagent"})
        assert "exec-slug" in ex.build_execute_instructions()

    def test_executor_snapshot(self):
        _make_standard_plan("exec-snap")
        ex = PlanExecutor(slug="exec-snap", workspace=Path.cwd(),
                          strategy_config={"execute": "tdd"})
        snap = ex.get_strategy_snapshot()
        assert snap["execute"]["name"] == "tdd"
        assert snap["execute"]["source"] == "builtin"
