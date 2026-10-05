"""Plan 系统单元测试：双轨分层、格式契约、策略插件引擎、git 提交范围。

Plan 3a Task 4 删掉了执行状态机（mark_task_*、start_plan_execution、PlanExecutor
的策略注入），Plan 3b Task A1 又删掉了 frontend/server/routers/sessions.py 的 8 个
plan REST 端点与它们专属的后端函数（pause/resume/skip/redo/abandon 与整条 ledger
链）。执行状态的唯一载体是 task_list，所以那些函数连同它们的测试一起消失了
（原 TestTaskStatusFile / TestLedger）。

留在这里的是物化路径的上游（create_plan / add_artifact / 任务解析 / 双轨校验 /
策略加载）与 _git_commit 的提交范围——create_plan / add_artifact /
append_tasks_to_plan 仍然调它，而它们正是 plan 批准时物化路径的上游。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agents.core.frontmatter import format_frontmatter
from agents.plan import plan_manager as pm
from agents.plan.plan_models import PlanGranularity, PlanStatus
from agents.plan.plan_mode import PlanModeManager


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


# ────────────────────────── 旧配置兼容 ──────────────────────────

def test_legacy_plan_strategies_key_in_config_is_ignored():
    """用户盘上的旧 config.json 带着已删除的 plan_strategies 段，必须能照常加载。

    Plan 模式简化把整套策略系统删了（strategy_loader / 5 个策略文件 /
    PlanStrategyConfig / /api/config/plan-strategies / client 的两个方法），
    但**不做迁移、不做检测**——AppConfig.from_dict 只 get 认识的键，残留段自然被忽略。
    这条钉的是「静默忽略」而不是「支持」：若有人日后给 from_dict 加严格校验，
    所有老用户升级后配置加载就会失败，而他们盘上那个键根本无法自行清除。
    """
    from agents.config import AppConfig

    cfg = AppConfig.from_dict({
        "endpoints": {}, "routing": {},
        "plan_strategies": {"grill_spec": "simple", "tasks": "structured",
                            "execute": "direct", "review": "none", "converge": "none"},
    })
    assert not hasattr(cfg, "plan_strategies")
    assert "plan_strategies" not in cfg.to_dict()


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


# ────────────────────── PlanStatus 反序列化面 ──────────────────────

class TestPlanStatusDeserialization:
    """PlanStatus 是「盘上已有 plan 目录」的反序列化面，7 个成员一个都不能少。

    _parse_plan_dir 用 `PlanStatus(meta.get("status", "proposed"))`，外面裹着一个
    blanket `except Exception: return None`（plan_manager.py）。少一个成员的后果是
    静默的：ValueError 被吞 → get_plan 返回 None → 那份计划在面板上凭空消失，且
    一条错误都不报。而 paused / ready_to_archive / completed / archived / abandoned
    这几个值**曾经真的被写过盘**（pause_plan、plan_complete 等已删除的写入方），
    所以「现在没有写入方」不等于「盘上没有」。

    此前 tests/ 里没有任何一处钉住非 proposed 的成员（grep PlanStatus / paused /
    ready_to_archive 零命中），删掉一个成员全套照旧绿。
    """

    @pytest.mark.parametrize("member", list(PlanStatus), ids=lambda m: m.value)
    def test_every_status_on_disk_round_trips(self, member, tmp_path, monkeypatch):
        plans_dir = tmp_path / ".mycode" / "plans"
        slug = f"st-{member.value.replace('_', '-')}"
        plan_dir = plans_dir / slug
        plan_dir.mkdir(parents=True)
        (plan_dir / "_meta.md").write_text(format_frontmatter({
            "slug": slug,
            "status": member.value,
            "priority": "P2",
            "created": "2026-01-01T00:00:00+00:00",
            "modified": "2026-01-01T00:00:00+00:00",
            "tags": [],
            "granularity": PlanGranularity.MINIMAL.value,
        }, ""), encoding="utf-8")
        monkeypatch.setattr(pm, "get_plans_dir", lambda: plans_dir)

        plan = pm.get_plan(slug)

        assert plan is not None, (
            f"status={member.value!r} 让整份 plan 从 get_plan 消失（blanket except "
            "把 ValueError 吞成了 None）——面板上会凭空少一份计划且零诊断"
        )
        assert plan.status is member
        assert plan.status.value == member.value
        assert plan.slug == slug

    def test_vocabulary_is_exactly_the_seven_members(self):
        """枚举本身也被钉住：加成员要过这里，删成员要过上面那条参数化测试。"""
        assert {m.value for m in PlanStatus} == {
            "proposed", "in-progress", "paused", "completed",
            "ready_to_archive", "archived", "abandoned",
        }

    def test_unknown_status_on_disk_still_vanishes_silently(self):
        """已记入 later plan 的遗留缺陷：这里只钉住**当前**行为，不是为它背书。

        真正的修法（未知 status 降级为可见哨兵值而不是 None + 记日志）不在本计划范围。
        这条测试存在是为了让那次修法必须**主动**改掉它，而不是悄悄发生。
        """
        plans_dir = Path.cwd() / ".mycode" / "plans"
        plan_dir = plans_dir / "st-unknown"
        plan_dir.mkdir(parents=True, exist_ok=True)
        (plan_dir / "_meta.md").write_text(format_frontmatter({
            "slug": "st-unknown",
            "status": "not-a-real-status",
            "priority": "P2",
            "created": "", "modified": "", "tags": [],
            "granularity": PlanGranularity.MINIMAL.value,
        }, ""), encoding="utf-8")

        assert pm.get_plan("st-unknown") is None


# ── A1：plan 提示词单轨化 + 负面清单 ────────────────────────────────────────
#
# 原来提示词分双轨（轻量轨 plan.md / 重量轨 spec+design+tasks），并把 grill-spec 与
# tasks 两个「阶段策略」插值进重量轨。而那套策略系统端到端**零消费者**：
# fetchPlanStrategies / savePlanStrategies 从来没有被任何组件调用过（没有设置界面），
# 唯一改法是手改 ~/.my-code/config.json；load_strategy 全仓只有一个调用方，
# 插值进的还是**非默认**的重量轨。用户裁定：任务是轻是重模型自会分辨，不必双轨。


def test_plan_prompt_is_single_track(tmp_path):
    prompt = PlanModeManager(workspace=tmp_path, session_id="s1").build_plan_mode_prompt()
    # ⚠️ 刻意**不**断言 "exit_plan_mode" 缺席：自动校验送审要到批次 B 才接上，
    # 在那之前删掉「调 exit_plan_mode」的指令会让 plan 模式根本没法送审——
    # 每个 commit 都必须是一个可用的产品状态。那条断言属于 B2（随工具一起删）。
    for banned in ("双轨", "轻量轨", "重量轨", "Phase 1", "Phase 2",
                   "spec.md", "design.md", "tasks.md"):
        assert banned not in prompt, banned


def test_plan_prompt_states_negative_list_not_positive(tmp_path):
    """负面清单（什么不能用），不是正面清单（你能用什么）。

    正面清单会随每加一个工具就漂移，而且和 registry 构成两份真相；负面清单只有几条
    且都是稳定的语义约束。本条的靶心是最后那组断言：如果有人日后好心加一句
    「你可以用 read_file / grep_search / list_files 探索」，它要红。
    """
    prompt = PlanModeManager(workspace=tmp_path, session_id="s1").build_plan_mode_prompt()
    assert "task_list" in prompt      # 明说 plan 模式下不可用
    assert "plan.md" in prompt        # 唯一产物
    assert "- [ ] 1." in prompt       # checkbox 格式（校验器按它判，两边必须一致）
    assert "验收" in prompt            # 每条任务必须有验收
    for tool in ("grep_search", "list_files", "outline_file", "web_search"):
        assert tool not in prompt, tool


def test_plan_prompt_keeps_the_h1_and_heading_contract(tmp_path):
    """两条格式约束是**真 bug 的防线**，删提示词时最容易顺手删掉：
    - 单行 `# 标题`：它是 slug 的唯一来源。Plan 3a 修过一个真 bug——`^#\\s+` 匹配不到
      `##`，而当时提示词只规定 `##` 小节，结果兜底 slug 与草稿目录同名撞车、
      create_plan 抛错被吞成 None、物化**静默什么都不落地**。
    - 精确的 `## 任务清单` 标题：`_strip_task_checklist_section` 只认它，切不动时
      整份清单会重复进对话。
    """
    prompt = PlanModeManager(workspace=tmp_path, session_id="s1").build_plan_mode_prompt()
    assert "# <标题>" in prompt
    assert "## 任务清单" in prompt
