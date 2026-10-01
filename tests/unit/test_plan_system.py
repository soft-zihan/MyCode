"""Plan 系统单元测试：双轨分层、任务状态落盘、格式契约、策略插件引擎。

Plan 3a Task 4 之后这里只剩「SDD 骨架」的覆盖面：执行状态机（mark_task_*、
start_plan_execution、PlanExecutor 的策略注入）已随 24 个 plan_* 工具一起删除，
执行状态的唯一载体是 task_list。留在这里的是仍被前端 REST 端点调用的那几个函数
（skip_task / redo_task / read_ledger / _update_task_status_in_file / _git_commit）
与物化路径的上游（create_plan / add_artifact / 任务解析 / 双轨校验 / 策略加载）。
"""

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


# ────────────────── 任务状态落盘（skip/redo 仍被前端 REST 调用）──────────────────

class TestTaskStatusFile:
    """`_update_task_status_in_file` 与它的两个仍存活的调用方。

    状态机那半（mark_task_in_progress/done/failed、全 done 自动 COMPLETED）已随
    Task 4 删除；skip_task / redo_task 因为 frontend/server/routers/sessions.py
    仍在调而保留（Plan 3b 处理），所以它们依赖的块内替换语义留在这里钉住。
    """

    def test_skip_redo_preconditions(self):
        _make_standard_plan("flow-pre", "### Task 1: A\n- **状态**: [ ] pending\n")
        assert not pm.redo_task("flow-pre", 1)  # pending 不可 redo
        assert pm.skip_task("flow-pre", 1)
        assert not pm.skip_task("flow-pre", 1)  # skipped 不可再 skip
        assert pm.redo_task("flow-pre", 1)

    def test_update_status_does_not_leak_across_blocks(self):
        _make_standard_plan("flow-block")
        assert pm.skip_task("flow-block", 1)
        content = pm.read_artifact("flow-block", "tasks.md")
        assert "[-] skipped" in content
        # Task 2 不受影响：块内替换不跨块
        assert pm.get_tasks("flow-block")[1].status == "pending"

    def test_simple_task_status_update_in_file(self):
        """checkbox 简单格式走的是另一条替换分支（没有 `### Task N:` 可定位）。"""
        _make_standard_plan("flow-simple", SIMPLE_TASKS)
        assert pm.skip_task("flow-simple", 1)
        content = pm.read_artifact("flow-simple", "tasks.md")
        assert "- [-] 1. 第一个任务" in content

    def test_pause_resume_abandon_round_trip(self):
        """pause/resume/abandon 保留 → `PlanStatus.PAUSED` 必须留在枚举里。

        Task 4 的裁剪清单要把 PlanStatus 收缩掉 PAUSED，但 pause_plan/resume_plan
        是前端仍在调的保留函数、两者都以 PAUSED 为前置/目标状态；删掉成员会让它们
        直接 AttributeError。这条把「保留函数依赖被保留的枚举成员」钉住。
        """
        _make_standard_plan("flow-pause")
        slug = "flow-pause"
        assert pm.get_plan(slug).status == PlanStatus.PROPOSED
        assert not pm.pause_plan(slug)                       # PROPOSED 不可暂停
        assert pm.update_plan_status(slug, PlanStatus.IN_PROGRESS)
        assert pm.pause_plan(slug)
        assert pm.get_plan(slug).status == PlanStatus.PAUSED
        assert not pm.pause_plan(slug)                       # 已暂停不可再暂停
        assert pm.resume_plan(slug)
        assert pm.get_plan(slug).status == PlanStatus.IN_PROGRESS
        assert pm.abandon_plan(slug)
        assert pm.get_plan(slug).status == PlanStatus.ABANDONED
        assert not pm.resume_plan(slug)                      # 已放弃不可恢复


# ────────────────────────── Ledger ──────────────────────────

class TestLedger:

    def test_ledger_records_lifecycle(self):
        _make_standard_plan("ledger-1", "### Task 1: A\n- **状态**: [x] done\n")
        pm.redo_task("ledger-1", 1)
        pm.skip_task("ledger-1", 1)
        # ledger 记录终态事件（in-progress 过渡不落账）
        events = [e.get("status") for e in pm.read_ledger("ledger-1")]
        for expected in ("redo", "skipped"):
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

    # ---- 轻量轨保留验收与缩进子项（Plan 3a Task 2）----
    #
    # 此前 checkbox_tasks_to_structured 只把 `- [ ] N. 描述` 转成 `### Task N: 描述`
    # + 状态，缩进子项整段丢弃——而解析器（_parse_structured_tasks）其实早就支持
    # `**验收**:`。不补这个洞，大多数 plan 物化出来的 detail 与 acceptance 都是空的，
    # 渐进式披露与验收闸门双双空转。

    def test_checkbox_with_acceptance_becomes_structured_acceptance(self):
        src = "## 任务清单\n- [ ] 1. 实现解析器\n  - 验收: pytest tests/test_p.py -q\n"
        out = PlanModeManager.checkbox_tasks_to_structured(src)
        assert "### Task 1: 实现解析器" in out
        assert "- **验收**: pytest tests/test_p.py -q" in out

    def test_checkbox_nested_bullets_become_task_body(self):
        src = (
            "## 任务清单\n"
            "- [ ] 1. 实现解析器\n"
            "  - 验收: pytest tests/test_p.py -q\n"
            "  - 改动: agents/parsers.py\n"
            "  - 注意: 空输入要返回空 dict 而不是 None\n"
        )
        out = PlanModeManager.checkbox_tasks_to_structured(src)
        assert "agents/parsers.py" in out
        assert "空输入要返回空 dict" in out

    def test_checkbox_without_subitems_still_converts(self):
        """向后兼容：老格式的纯一行 checkbox 仍须能转。"""
        out = PlanModeManager.checkbox_tasks_to_structured("- [ ] 1. 简单任务\n- [ ] 2. 另一个\n")
        assert "### Task 1: 简单任务" in out
        assert "### Task 2: 另一个" in out

    def test_parsed_task_carries_acceptance_end_to_end(self):
        """转换 → 解析的整条链路：验收必须活着走到 Task.acceptance。"""
        src = "- [ ] 1. 实现解析器\n  - 验收: pytest tests/test_p.py -q\n"
        structured = PlanModeManager.checkbox_tasks_to_structured(src)
        tasks = pm._parse_structured_tasks(structured)
        assert len(tasks) == 1
        assert tasks[0].acceptance == "pytest tests/test_p.py -q"

    def test_numbering_survives_non_contiguous_ids(self):
        out = PlanModeManager.checkbox_tasks_to_structured("- [ ] 3. 第三个\n- [ ] 7. 第七个\n")
        assert "### Task 3: 第三个" in out
        assert "### Task 7: 第七个" in out

    def test_wrapper_and_all_status_markers_survive(self):
        """`### Task` 与 TASKS START/END 包裹是 handle_plan_system_integration 的
        依赖（plan_mode.py 的 `if "### Task" not in structured_tasks: return None`），
        五个状态标记的映射也不能在改造中丢掉。"""
        out = PlanModeManager.checkbox_tasks_to_structured(SIMPLE_TASKS)
        assert out.startswith("<!-- TASKS START -->")
        assert "<!-- TASKS END -->" in out
        assert "### Task" in out
        assert "[ ] pending" in out
        assert "[x] done" in out
        assert "[~] in-progress" in out
        assert "[!] failed" in out
        assert "[-] skipped" in out
        # 解析侧同样认得这五个状态（转换 → 解析全链路）
        assert {t.id: t.status for t in pm._parse_structured_tasks(out)} == {
            1: "pending", 2: "done", 3: "in-progress", 4: "failed", 5: "skipped",
        }

    def test_indent_detection_is_relative_not_hardcoded_two_spaces(self):
        """缩进判定用「前导空白长于父行」，不是固定两空格。

        四空格、制表符、以及父行本身就带缩进（任务清单嵌在别的列表里）都必须
        照样认出子项；而同级或更浅的行——尤其是后面的 `## 验收` 小节——绝不能
        被吸进最后一个任务块。
        """
        four = PlanModeManager.checkbox_tasks_to_structured(
            "- [ ] 1. A\n    - 验收: pytest -k a\n")
        assert "- **验收**: pytest -k a" in four

        tabbed = PlanModeManager.checkbox_tasks_to_structured(
            "- [ ] 1. A\n\t- 验收: pytest -k t\n")
        assert "- **验收**: pytest -k t" in tabbed

        nested = PlanModeManager.checkbox_tasks_to_structured(
            "1. 外层\n   - [ ] 2. 内层任务\n     - 验收: pytest -k n\n")
        assert "### Task 2: 内层任务" in nested
        assert "- **验收**: pytest -k n" in nested

        # 同级/更浅的行不进任务块：`## 验收` 小节属于整份 plan，不属于任务 1
        sibling = PlanModeManager.checkbox_tasks_to_structured(
            "- [ ] 1. A\n  - 验收: pytest -k a\n\n## 验收\n- 跑全套测试\n")
        assert "- **验收**: pytest -k a" in sibling
        assert "跑全套测试" not in sibling
        assert pm.count_tasks(sibling) == 1

    def test_subitem_without_key_is_kept_as_bare_bullet(self):
        """无键的 bullet 也要进块正文（`- value` 形式），别只认 `key: value`。"""
        out = PlanModeManager.checkbox_tasks_to_structured(
            "- [ ] 1. A\n  - 先跑迁移再改模型\n")
        assert "先跑迁移再改模型" in out

    def test_value_containing_a_colon_is_not_misparsed_as_a_key(self):
        """`- 见 https://x` 里的冒号不能被当成 key/value 分隔（key 含空格即非键）。"""
        out = PlanModeManager.checkbox_tasks_to_structured(
            "- [ ] 1. A\n  - 见 https://example.com/doc\n")
        assert "见 https://example.com/doc" in out


# ────────────────────────── 策略加载与插件引擎 ──────────────────────────

class TestStrategies:
    """策略加载器。

    三个用例原本打在 `execute` 阶段上，而 `strategies/execute|review|converge/`
    随 `plan_task_*` 协议一起在 Task 4 删除了（它们的唯一消费者 PlanExecutor 也没了），
    所以改打仍存活的 `grill-spec`——那才是 build_plan_mode_prompt 真正加载的两个
    阶段之一。strategy_loader 本身与 DEFAULT_STRATEGIES 的五键**不动**：
    PlanStrategyConfig 经 `/api/config/plan-strategies` 暴露给前端，收缩到两阶段是
    Plan 3b 的事（会改 openapi.json）。
    """

    def test_load_builtin_default(self):
        content, source, resolved = load_strategy(
            "grill-spec", "simple", Path.cwd(), Path.cwd() / ".mycode/plans/x")
        assert source == "builtin" and resolved == "simple"
        assert "需求分析" in content
        # {plan_dir} 占位符被替换掉了
        assert "{plan_dir}" not in content

    def test_project_override_wins(self):
        custom_dir = Path.cwd() / ".mycode" / "plan-strategies" / "grill-spec"
        custom_dir.mkdir(parents=True, exist_ok=True)
        (custom_dir / "simple.md").write_text("PROJECT CUSTOM", encoding="utf-8")
        content, source, resolved = load_strategy(
            "grill-spec", "simple", Path.cwd(), Path.cwd() / ".mycode/plans/x")
        assert source == "project" and content.strip() == "PROJECT CUSTOM"

    def test_missing_strategy_falls_back(self):
        content, source, resolved = load_strategy(
            "grill-spec", "no-such-strategy", Path.cwd(), Path.cwd() / ".mycode/plans/x")
        assert resolved == DEFAULT_STRATEGIES["grill-spec"] == "simple"
        assert source == "fallback"
        assert content.strip()

    def test_app_config_key_translation(self):
        cfg = strategy_config_from_app_config()
        assert set(cfg.keys()) == set(DEFAULT_STRATEGIES.keys())


# ────────────────────────── Git 提交范围 ──────────────────────────

class TestGitCommitScope:
    """_git_commit 只允许提交 .mycode/plans/ 下的变更，不得卷入仓库其他文件。"""

    def _init_repo(self, tmp_path: Path) -> None:
        import subprocess
        subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
        subprocess.run(["git", "config", "user.email", "test@test"], cwd=tmp_path, check=True)
        subprocess.run(["git", "config", "user.name", "test"], cwd=tmp_path, check=True)
        (tmp_path / "README.md").write_text("init")
        subprocess.run(["git", "add", "README.md"], cwd=tmp_path, check=True)
        subprocess.run(["git", "commit", "-qm", "init"], cwd=tmp_path, check=True)

    def test_git_commit_only_plans_dir(self, tmp_path, monkeypatch):
        import subprocess
        self._init_repo(tmp_path)
        plans_dir = tmp_path / ".mycode" / "plans"
        (plans_dir / "slug-a").mkdir(parents=True)
        (plans_dir / "slug-a" / "plan.md").write_text("# Plan A")
        (tmp_path / "unrelated.py").write_text("secret WIP")

        monkeypatch.setattr(pm, "get_plans_dir", lambda: plans_dir)
        pm._git_commit("plan: create slug-a")

        files = subprocess.run(
            ["git", "show", "--name-only", "--format=", "HEAD"],
            cwd=tmp_path, capture_output=True, text=True, check=True,
        ).stdout.split()
        assert files == [".mycode/plans/slug-a/plan.md"]
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=tmp_path, capture_output=True, text=True, check=True,
        ).stdout
        assert "unrelated.py" in status

    def test_git_commit_no_change_no_commit(self, tmp_path, monkeypatch):
        import subprocess
        self._init_repo(tmp_path)
        plans_dir = tmp_path / ".mycode" / "plans"
        plans_dir.mkdir(parents=True)
        monkeypatch.setattr(pm, "get_plans_dir", lambda: plans_dir)
        before = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=tmp_path, capture_output=True, text=True, check=True,
        ).stdout.strip()
        pm._git_commit("plan: noop")
        after = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=tmp_path, capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert before == after
