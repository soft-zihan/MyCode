"""plan 批准后物化进 task_list。"""
from __future__ import annotations

import pytest

from agents.core.workspace import reset_workspace, set_workspace
from agents.plan import plan_tool_executor
from agents.plan.plan_manager import add_artifact, append_tasks_to_plan, create_plan
from agents.plan.plan_mode import PlanModeManager
from agents.plan.plan_models import PlanGranularity
from agents.plan.plan_tool_executor import _materialize_plan_into_task_list
from agents.tools.task_store import list_tasks

SLUG = "mat-test"


@pytest.fixture
def ws(tmp_path):
    token = set_workspace(tmp_path)
    yield tmp_path
    reset_workspace(token)


def _make_plan(tasks_md: str) -> None:
    create_plan(slug=SLUG, granularity=PlanGranularity.STANDARD)
    add_artifact(SLUG, "tasks.md", tasks_md)


STRUCTURED = """<!-- TASKS START -->
### Task 1: 实现解析器
- **状态**: [ ] pending
- **验收**: pytest tests/test_p.py -q
- **文件**: agents/parsers.py
- **函数**: normalize_marker

空输入要返回空 dict 而不是 None，下游会直接下标访问。

### Task 2: 补文档
- **状态**: [ ] pending

写 README 的用法小节。
<!-- TASKS END -->
"""

# 同一 session 第二次批准时新增的那一块（编号接着上面，与 append_tasks_to_plan
# 直接拼接 tasks.md 的形状一致）。
SECOND_BATCH = """<!-- TASKS START -->
### Task 3: 加缓存层
- **状态**: [ ] pending
- **验收**: pytest tests/test_cache.py -q

缓存键要带版本号，否则升级后会读到旧值。

### Task 4: 写迁移脚本
- **状态**: [ ] pending
<!-- TASKS END -->
"""

# 七个已知标记全带上：body 收集漏掉任何一个，这条都会红（Finding 6）。
ALL_MARKERS = """<!-- TASKS START -->
### Task 1: 全标记的一条
- **状态**: [ ] pending
- **验收**: pytest tests/test_all.py -q
- **文件**: agents/everywhere.py
- **函数**: normalize_marker
- **接口**: POST /v1/things
- **错误**: "不该出现"
- **重试次数**: 0

真正的块正文只此一行。
<!-- TASKS END -->
"""

MIXED_STATUS = """<!-- TASKS START -->
### Task 1: 已完成的那条
- **状态**: [x] done

### Task 2: 进行中的那条
- **状态**: [~] in-progress

### Task 3: 失败的那条
- **状态**: [!] failed
- **错误**: "boom"

### Task 4: 跳过的那条
- **状态**: [-] skipped
<!-- TASKS END -->
"""

# 轻量轨 checkbox 没有缩进子项时的形状：转换器只产出标题 + 状态，两者都被
# _compose_detail 排除 → detail 为空 → needs_disclosure False → 什么都不会注入。
NO_BODY = """<!-- TASKS START -->
### Task 1: 光有个标题
- **状态**: [ ] pending
<!-- TASKS END -->
"""

SIMPLE_CHECKBOX = """## 任务清单

- [ ] 1. 第一个任务
- [ ] 2. 第二个任务
"""

MINIMAL_PLAN_MD = """# Fix Login Timeout

## 背景

登录请求要等 30s 才超时，用户全程看不到任何提示。

## 方案

把超时收到 10s，并在前端弹一条提示。

## 任务清单

- [ ] 1. 收紧超时
  - 验收: pytest tests/test_login.py -q
  - 注意: 别动重试次数
- [ ] 2. 加超时提示

## 验收

pytest tests/test_login.py -q 全绿。
"""

SPEC_MD = "# Spec: Fix Login Timeout\n\n## 验收标准\n\n- pytest 全绿\n"
DESIGN_MD = "## 设计\n\n把超时收到 10s，前端加一条提示。\n"


def _draft_mgr(tmp_path, *, plan_md=None, spec_md=None, design_md=None, tasks_md=None):
    """造一个带草稿产物的真 PlanModeManager（_validate_and_load_draft 只读它）。"""
    mgr = PlanModeManager(workspace=tmp_path, session_id="s1")
    plan_dir = tmp_path / "plandir"
    plan_dir.mkdir(parents=True, exist_ok=True)
    for name, text in (("plan.md", plan_md), ("spec.md", spec_md),
                       ("design.md", design_md), ("tasks.md", tasks_md)):
        if text is not None:
            (plan_dir / name).write_text(text, encoding="utf-8")
    mgr.plan_dir = plan_dir
    return mgr


class _FakeAgent:
    """只实现 _finalize_plan_exit 真正碰到的那几个属性/方法。"""

    def __init__(self, session):
        self.session = session
        self.permission_mode = "plan"
        self._system_prompt = "PLAN-SYS"
        self._base_system_prompt = "BASE-SYS"
        self.mode_events = 0

    def _emit_permission_mode_event(self) -> None:
        self.mode_events += 1


def _new_session():
    from agents.core.session import Session
    return Session("s1", origin="sub_agent")


def _finalize(agent, mgr):
    """跑真的 _validate_and_load_draft + _finalize_plan_exit（生产路径）。"""
    from agents.plan.plan_tool_executor import _finalize_plan_exit, _validate_and_load_draft
    return _finalize_plan_exit(agent, mgr, _validate_and_load_draft(mgr), "execute")


# ────────────────────────── 基础物化 ──────────────────────────

def test_materializes_every_task_in_order(ws):
    _make_plan(STRUCTURED)
    assert _materialize_plan_into_task_list("s1", SLUG) == (2, True)
    assert [t.content for t in list_tasks("s1")] == ["实现解析器", "补文档"]


def test_acceptance_maps_across(ws):
    _make_plan(STRUCTURED)
    _materialize_plan_into_task_list("s1", SLUG)
    got = list_tasks("s1")
    assert got[0].acceptance == "pytest tests/test_p.py -q"
    assert got[1].acceptance == ""


def test_detail_composes_file_function_and_body(ws):
    _make_plan(STRUCTURED)
    _materialize_plan_into_task_list("s1", SLUG)
    detail = list_tasks("s1")[0].detail
    assert "agents/parsers.py" in detail
    # Finding 5：函数名刻意不是文件路径的子串（旧 fixture 用 `parse`，而
    # `agents/parsers.py` 里就含 "parse"，于是把 function 从 _compose_detail 里
    # 整个删掉也照样绿——断言毫无判别力）。
    assert "normalize_marker" in detail
    assert "空输入要返回空 dict" in detail
    # Finding 6 的最低要求：compose 这条自己就得带标记泄漏的负断言
    assert "- **状态**" not in detail
    assert "- **文件**" not in detail


def test_detail_leaks_no_known_markers(ws):
    """Finding 6：七个标记一个都不许进 detail。

    此前 detail 上唯一的负断言只覆盖 `验收`。把 `_BODY_SKIP_LINE_RE`
    （plan_manager.py）里的 `状态|错误|重试次数|文件|函数|接口` 全删掉，旧套件
    依然全绿——状态噪声于是会流进每一条 detail，进而流进常驻 S 与注入预算。
    断言的是**标记形态**（`- **文件**`）而不是词本身：detail 里本来就有
    `涉及文件: …`/`涉及函数: …` 这样的散文标签。
    """
    _make_plan(ALL_MARKERS)
    _materialize_plan_into_task_list("s1", SLUG)
    detail = list_tasks("s1")[0].detail
    for marker in ("状态", "验收", "文件", "函数", "接口", "错误", "重试次数"):
        assert f"- **{marker}**" not in detail, marker
        assert f"**{marker}**:" not in detail, marker
    # 标记行的载荷也不该漏进来（file/function/interface 的载荷**会**以散文标签
    # 形式进 detail，那是 _compose_detail 的本职，不在此列）
    assert "pending" not in detail
    assert "不该出现" not in detail
    # 真正的块正文要在
    assert "真正的块正文只此一行。" in detail


def test_detail_does_not_duplicate_acceptance(ws):
    """acceptance 已单独成字段，detail 里不该再抄一遍。"""
    _make_plan(STRUCTURED)
    _materialize_plan_into_task_list("s1", SLUG)
    assert "pytest tests/test_p.py -q" not in list_tasks("s1")[0].detail


def test_origin_seq_is_none_so_first_detail_gets_injected(ws):
    """物化路径的核心不变量：detail 来自磁盘，不在模型上下文里，必须注入。"""
    _make_plan(STRUCTURED)
    _materialize_plan_into_task_list("s1", SLUG)
    assert all(t.detail_origin_seq is None for t in list_tasks("s1"))


def test_all_materialized_tasks_start_pending(ws):
    _make_plan(STRUCTURED)
    _materialize_plan_into_task_list("s1", SLUG)
    assert all(t.status == "pending" for t in list_tasks("s1"))


def test_status_mapping(ws):
    _make_plan(MIXED_STATUS)
    _materialize_plan_into_task_list("s1", SLUG)
    assert [t.status for t in list_tasks("s1")] == [
        "completed", "in_progress", "failed", "skipped",
    ]


def test_existing_tasks_are_appended_not_clobbered(ws):
    """追加模式：session 已有清单时物化必须接在后面。"""
    from agents.tools.task_store import add_task
    add_task("s1", "已有任务")
    _make_plan(STRUCTURED)
    _materialize_plan_into_task_list("s1", SLUG)
    assert [t.content for t in list_tasks("s1")] == ["已有任务", "实现解析器", "补文档"]


def test_no_tasks_returns_zero_and_creates_nothing(ws):
    _make_plan("<!-- TASKS START -->\n<!-- TASKS END -->\n")
    assert _materialize_plan_into_task_list("s1", SLUG) == (0, True)
    assert list_tasks("s1") == []


def test_unknown_slug_returns_zero(ws):
    assert _materialize_plan_into_task_list("s1", "nope") == (0, True)


# ────────────── Finding 1：二次批准只物化新增的那一块 ──────────────

def test_second_approval_materializes_only_the_new_block(ws):
    """同一 session 第二次批准：tasks.md 已经是「旧 + 新」的合并体。

    链条是确定性的，不是奇异路径：session.plan_slug 只设不清（跨 clear、跨重启
    存活），而 Task 3 之后**再没有任何东西推进 plan 状态**（start_plan_execution
    不再被调用，create_plan 写的是 PROPOSED），PROPOSED/IN_PROGRESS/COMPLETED
    全都过 `not in ("archived", "abandoned")` 那道闸 → handle_plan_system_integration
    必走 append。按 slug 读于是读到合并体，上一轮已物化过的任务被再物化一遍
    （新 id + pending），task_list 翻倍：S 常驻里长期挂着 N 条陈旧重复（正是本设计
    要消灭的那笔 token 账），find_focus 还可能落到已完成工作的 pending 克隆上、
    把它的 detail 重新注入、邀模型把活重做一遍。
    """
    _make_plan(STRUCTURED)
    assert _materialize_plan_into_task_list("s1", SLUG) == (2, True)

    append_tasks_to_plan(SLUG, SECOND_BATCH)     # tasks.md 现在是合并体
    assert len(list_tasks("s1")) == 2

    # 只物化本次新增的那一块
    assert _materialize_plan_into_task_list("s1", SLUG, tasks_md=SECOND_BATCH) == (2, True)
    contents = [t.content for t in list_tasks("s1")]
    assert contents == ["实现解析器", "补文档", "加缓存层", "写迁移脚本"]
    assert len(contents) == len(set(contents)), "task_list 里出现了重复 content"


def test_tasks_md_bypasses_the_file_entirely(ws):
    """给了 tasks_md 就不读盘：slug 不存在也照样物化。

    顺带钉住解析走的是 get_tasks 的同一份逻辑（结构化优先、回退 checkbox 简单
    格式），所以重量轨与轻量轨两条都能从 tasks_md 物化。
    """
    assert _materialize_plan_into_task_list(
        "s1", "no-such-slug", tasks_md=SIMPLE_CHECKBOX) == (2, True)
    assert [t.content for t in list_tasks("s1")] == ["第一个任务", "第二个任务"]


def test_tasks_md_empty_string_does_not_fall_back_to_the_merged_file(ws):
    """空串是「本次没有新增」，不是「没给」——不许退回按 slug 读合并体。"""
    _make_plan(STRUCTURED)
    assert _materialize_plan_into_task_list("s1", SLUG, tasks_md="") == (0, True)
    assert list_tasks("s1") == []


def test_double_approval_through_finalize_does_not_duplicate(ws, tmp_path):
    """Finding 1 的调用方一侧：_finalize_plan_exit 在 action=="appended" 时必须
    把 draft.tasks（本次新增的那一块）交给物化，而不是让它按 slug 读合并体。

    两轮都走真的 handle_plan_system_integration：第一轮 created，第二轮因为
    session.plan_slug 已在（同一个 Session 对象）而自动落到 append 分支。
    """
    session = _new_session()

    mgr1 = _draft_mgr(tmp_path, spec_md=SPEC_MD, design_md=DESIGN_MD, tasks_md=STRUCTURED)
    agent1 = _FakeAgent(session)
    msg1 = _finalize(agent1, mgr1)
    assert [t.content for t in list_tasks("s1")] == ["实现解析器", "补文档"]
    assert "2 条任务已物化" in msg1
    assert session.plan_slug, "created 路径本该把 slug 挂到 session 上"
    assert agent1.mode_events == 1

    slug = session.plan_slug
    mgr2 = _draft_mgr(tmp_path, spec_md=SPEC_MD, design_md=DESIGN_MD, tasks_md=SECOND_BATCH)
    msg2 = _finalize(_FakeAgent(session), mgr2)

    contents = [t.content for t in list_tasks("s1")]
    assert contents == ["实现解析器", "补文档", "加缓存层", "写迁移脚本"], contents
    assert len(contents) == len(set(contents)), "第二次批准把整个合并体重物化了一遍"
    assert "2 条任务已物化" in msg2, msg2
    # 合并体确实躺在盘上（证明第二轮真的走了 append 而不是 created）
    from agents.plan.plan_manager import get_tasks
    assert len(get_tasks(slug)) == 4


def test_finalize_passes_tasks_md_only_on_the_appended_path(ws, tmp_path, monkeypatch):
    """直接钉住调用契约：action=="appended" → tasks_md=draft.tasks；
    action=="created" → tasks_md 不传（照旧按 slug 读）。"""
    seen: list[tuple[str, str | None]] = []
    real = plan_tool_executor._materialize_plan_into_task_list

    def spy(session_id, slug, tasks_md=None):
        seen.append((slug, tasks_md))
        return real(session_id, slug, tasks_md=tasks_md)

    monkeypatch.setattr(plan_tool_executor, "_materialize_plan_into_task_list", spy)

    session = _new_session()
    mgr1 = _draft_mgr(tmp_path, spec_md=SPEC_MD, design_md=DESIGN_MD, tasks_md=STRUCTURED)
    _finalize(_FakeAgent(session), mgr1)
    assert seen[-1][1] is None, "created 路径不该传 tasks_md"

    mgr2 = _draft_mgr(tmp_path, spec_md=SPEC_MD, design_md=DESIGN_MD, tasks_md=SECOND_BATCH)
    _finalize(_FakeAgent(session), mgr2)
    assert seen[-1][1] == SECOND_BATCH, "appended 路径必须把本次新增的那一块交下去"


# ────────────── Finding 2：对话版计划文本 ──────────────

def test_standard_conversation_plan_omits_tasks(ws, tmp_path):
    from agents.plan.plan_tool_executor import _validate_and_load_draft
    mgr = _draft_mgr(tmp_path, spec_md=SPEC_MD, design_md=DESIGN_MD, tasks_md=STRUCTURED)
    draft = _validate_and_load_draft(mgr)
    assert draft.granularity == "standard"
    assert "## Spec" in draft.conversation_plan
    assert "## Design" in draft.conversation_plan
    assert "验收标准" in draft.conversation_plan
    assert "## Tasks" not in draft.conversation_plan
    assert "实现解析器" not in draft.conversation_plan
    # 人看的那份一个字不动：批准弹窗与 CLI 确认框都要能看到任务清单
    assert "## Tasks" in draft.full_plan
    assert "实现解析器" in draft.full_plan
    assert "## Spec" in draft.full_plan and "## Design" in draft.full_plan


def test_minimal_conversation_plan_omits_checklist(ws, tmp_path):
    """轻量轨是默认且占多数的那条——它没有 `## Tasks` 小节可省，任务清单是
    plan.md 里的一段（`## 任务清单`），只能按标题切出来。只删 `## Tasks` 的
    「修复」在这条轨上静默无效。"""
    from agents.plan.plan_tool_executor import _validate_and_load_draft
    mgr = _draft_mgr(tmp_path, plan_md=MINIMAL_PLAN_MD)
    draft = _validate_and_load_draft(mgr)
    assert draft.granularity == "minimal"
    for section in ("## 背景", "## 方案", "## 验收"):
        assert section in draft.conversation_plan, section
    assert "30s" in draft.conversation_plan        # 背景正文活着
    assert "全绿" in draft.conversation_plan        # 任务清单之后的 ## 验收 活着
    assert "- [ ]" not in draft.conversation_plan
    assert "收紧超时" not in draft.conversation_plan
    assert "## 任务清单" not in draft.conversation_plan
    # full_plan 仍是整份 plan.md，checkbox 清单在
    assert "- [ ] 1. 收紧超时" in draft.full_plan
    assert "## 任务清单" in draft.full_plan


def test_result_message_carries_conversation_plan_not_full_plan(ws, tmp_path):
    """`## Approved Plan:` 那一段进的是**对话**，S 已常驻上下文尾部，再把任务清单
    抄一遍就是 format_plan_tasks_block（本任务已删）那笔重复账。"""
    from agents.plan.plan_tool_executor import _validate_and_load_draft
    mgr = _draft_mgr(tmp_path, spec_md=SPEC_MD, design_md=DESIGN_MD, tasks_md=STRUCTURED)
    draft = _validate_and_load_draft(mgr)
    msg = _finalize(_FakeAgent(_new_session()), mgr)
    assert f"## Approved Plan:\n{draft.conversation_plan}" in msg
    assert "## Spec" in msg and "## Design" in msg
    assert "实现解析器" not in msg, "任务清单又进对话了"
    assert "补文档" not in msg


@pytest.mark.parametrize("heading", [
    "## 任务清单",
    "##任务清单",
    "## 任务清单   ",
    "## 任务清单（checkbox：- [ ] 1. 描述）",
    "###  任务清单",
])
def test_strip_task_checklist_section_tolerates_heading_variants(heading):
    from agents.plan.plan_tool_executor import _strip_task_checklist_section
    text = f"## 背景\n\nB\n\n{heading}\n\n- [ ] 1. 做点什么\n\n## 验收\n\nA\n"
    out = _strip_task_checklist_section(text)
    assert "- [ ]" not in out, out
    assert "做点什么" not in out, out
    assert "## 背景" in out and "B" in out
    assert "## 验收" in out and "A" in out


def test_strip_task_checklist_section_keeps_following_section():
    from agents.plan.plan_tool_executor import _strip_task_checklist_section
    out = _strip_task_checklist_section(MINIMAL_PLAN_MD)
    assert "## 背景" in out and "## 方案" in out
    assert "## 验收" in out and "全绿" in out      # 任务清单之后的小节必须活下来
    assert "- [ ]" not in out
    assert "## 任务清单" not in out


def test_strip_task_checklist_section_noop_without_heading():
    """没有 `## 任务清单` 标题的 plan 必须逐字原样返回。"""
    from agents.plan.plan_tool_executor import _strip_task_checklist_section
    text = "# T\n\n## 背景\n\n这个 plan 没写任务清单小节。\n"
    assert _strip_task_checklist_section(text) == text


def test_strip_task_checklist_section_at_eof():
    """任务清单在文末（没有后继 `##` 标题）：切到字符串末尾。"""
    from agents.plan.plan_tool_executor import _strip_task_checklist_section
    text = "## 背景\n\nB\n\n## 任务清单\n\n- [ ] 1. 做点什么\n"
    out = _strip_task_checklist_section(text)
    assert "- [ ]" not in out and "做点什么" not in out
    assert "## 背景" in out and "B" in out


# ────────────── Finding 3：不许承诺不会发生的注入 ──────────────

def test_result_message_promises_injection_when_detail_exists(ws, tmp_path):
    mgr = _draft_mgr(tmp_path, spec_md=SPEC_MD, design_md=DESIGN_MD, tasks_md=STRUCTURED)
    msg = _finalize(_FakeAgent(_new_session()), mgr)
    assert "自动注入" in msg


def test_result_message_does_not_promise_injection_for_empty_detail(ws, tmp_path):
    """轻量轨 checkbox 不带缩进子项时合法通过校验（只要求 task_count > 0），
    转换器于是只产出 `### Task N: 描述` + `- **状态**: …`，两者都被 body 收集
    排除 → _compose_detail 返回 "" → needs_disclosure 对空 detail 恒为 False →
    **什么都不会注入**。旧文案却无条件说「会在下一次模型调用时自动注入」。
    """
    mgr = _draft_mgr(tmp_path, spec_md=SPEC_MD, design_md=DESIGN_MD, tasks_md=NO_BODY)
    msg = _finalize(_FakeAgent(_new_session()), mgr)
    assert "1 条任务已物化" in msg, msg
    assert "自动注入" not in msg, msg
    assert "没有记录详细执行方案" in msg, msg
    # 事实核对：焦点条的 detail 确实是空的
    from agents.tools.task_store import find_focus
    assert find_focus(list_tasks("s1")).detail == ""


# ────────────── Finding 4：截断要可见、计数不能差一条 ──────────────

def test_complete_is_true_on_the_happy_path(ws):
    _make_plan(STRUCTURED)
    assert _materialize_plan_into_task_list("s1", SLUG) == (2, True)


def test_count_reflects_store_when_status_update_fails(ws, monkeypatch):
    """旧实现 `count += 1` 在 update_task **之后**：update 失败时 store 里已经躺着
    一条任务而 count 不含它（差一条）。计数必须跟着「store 里真有什么」走。
    """
    _make_plan(MIXED_STATUS)

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(plan_tool_executor, "update_task", boom)
    count, complete = _materialize_plan_into_task_list("s1", SLUG)
    assert complete is False
    assert count == len(list_tasks("s1")) == 1


def test_result_message_flags_truncation(ws, tmp_path, monkeypatch):
    """截断必须对**模型**可见：旧实现只往 stderr 说一声，消息里照样是
    「N 条任务已物化」，读起来像最终结果。"""
    real_add = plan_tool_executor.add_task
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("disk full")
        return real_add(*args, **kwargs)

    monkeypatch.setattr(plan_tool_executor, "add_task", flaky)
    mgr = _draft_mgr(tmp_path, spec_md=SPEC_MD, design_md=DESIGN_MD, tasks_md=STRUCTURED)
    msg = _finalize(_FakeAgent(_new_session()), mgr)
    assert "1 条任务已物化" in msg, msg
    assert "可能不完整" in msg, msg


# ────────────── Finding 7：不变量的行为级钉法 ──────────────

def test_materialized_detail_reaches_the_model_via_disclosure(ws):
    """端到端：物化 → ensure_focus_detail_visible → 首条方案被注入。

    Finding 7：visible_seqs 必须非空，且物化必须发生在有可见事件**之后**。
    `Session.system_prompt` setter 不落事件（session.py），所以旧写法下
    visible_seqs == ()，而 needs_disclosure 在空可见集里对**任何** int
    detail_origin_seq 都返回 True —— 把物化改成传 current_seq（反转不变量）旧测试
    照样绿，等于没钉。生产里反转实现会传「承载本次 tool_calls 的 assistant 事件
    seq」，那个 seq 在批准后第一次模型调用时一定可见，所以这里先落一条真事件再物化。
    """
    from agents.tools.task_disclosure import ensure_focus_detail_visible
    from agents.tools.task_store import find_focus
    _make_plan(STRUCTURED)
    s = _new_session()
    s.system_prompt = "SYS"
    s.append("session/plan_linked", {"plan_slug": SLUG})
    assert s.visible_seqs, "前置条件：可见集非空，否则本条毫无判别力"

    _materialize_plan_into_task_list("s1", SLUG)
    focus = find_focus(list_tasks("s1"))
    assert focus.detail_origin_seq is None, "不变量：物化不传 current_seq"

    assert ensure_focus_detail_visible(s) is True
    injected = [e for e in s.events if e.get("type") == "memory_injection"]
    assert len(injected) == 1
    assert "空输入要返回空 dict" in injected[0]["content"]
    assert "实现解析器" in injected[0]["content"]
    # 幂等：注入后 detail_origin_seq 记到了新事件 seq 且该事件可见 → 不再注入
    assert ensure_focus_detail_visible(s) is False
    assert len([e for e in s.events if e.get("type") == "memory_injection"]) == 1


def test_detail_pinned_to_a_visible_seq_is_not_reinjected(ws):
    """Finding 7 的反面用例——这条才是能把「不变量被反转」钉住的那条。

    detail_origin_seq 落在可见集里 → needs_disclosure False → 不注入。物化路径
    永远走不到这里（它刻意不传 current_seq），所以这条构造的是「如果传了」的那个
    世界：只有上面那条端到端用例时，那个世界一样能过。
    """
    from agents.tools.task_disclosure import ensure_focus_detail_visible
    from agents.tools.task_store import add_task
    s = _new_session()
    s.system_prompt = "SYS"
    for _ in range(8):
        s.append("session/plan_linked", {"plan_slug": SLUG})   # seq 0..7 全部可见
    add_task("s1", content="实现解析器", detail="空输入要返回空 dict", current_seq=7)
    assert 7 in s.visible_seqs
    assert ensure_focus_detail_visible(s) is False
    assert [e for e in s.events if e.get("type") == "memory_injection"] == []


# ────────────── 收口 A：批准落地后清掉草稿目录 ──────────────

_MINIMAL_HEAD = "# Fix Login Timeout\n\n## 任务清单\n\n"


def _approve_round(session, workspace, n: int, desc: str, subitems: str = ""):
    """跑一轮真批准，草稿目录用 generate_plan_dir() 的真路径（两轮同路径）。

    「追加模式」照提示词教的样子来（plan_mode.py:127-128：目录已存在就先 read_file、
    在任务清单末尾追加）：plan.md 幸存就读出来追加，被清空了才从头写。所以第二轮的
    plan.md 里到底有什么，完全由第一轮清没清干净决定——这正是本组要钉的因果，而不是
    把结论写进 fixture。
    """
    mgr = PlanModeManager(workspace=workspace, session_id="s1")
    plan_dir = mgr.generate_plan_dir()
    p = plan_dir / "plan.md"
    head = p.read_text(encoding="utf-8").rstrip("\n") + "\n" if p.exists() else _MINIMAL_HEAD
    p.write_text(head + f"- [ ] {n}. {desc}\n" + subitems, encoding="utf-8")
    mgr.plan_dir = plan_dir
    return plan_dir, _finalize(_FakeAgent(session), mgr)


def test_draft_dir_is_cleared_after_successful_approval(ws, tmp_path):
    """草稿目录 `<workspace>/.mycode/plans/plan-<session_id>`（plan_mode.py:57-61）
    此前**从不被清**：_finalize_plan_exit 只把属性置 None，那条路径上没有任何
    rmtree/unlink。它于是跨轮幸存，成为二次批准重复物化的源头。
    """
    draft_dir, msg = _approve_round(_new_session(), tmp_path, 1, "收紧超时")
    assert "1 条任务已物化" in msg, msg
    assert not draft_dir.exists(), "批准落地后草稿目录必须清掉"


def test_second_approval_reusing_draft_dir_does_not_duplicate(ws, tmp_path):
    """收口 A 的后果级钉法：同一路径的草稿目录被第二轮复用。

    轻量轨的转换器会把幸存 plan.md 里的**每个** checkbox 重新转一遍，于是第二轮
    draft.tasks = 旧 + 新，上一轮已物化过的任务被再物化一遍（新 id + pending）。
    """
    session = _new_session()
    dir1, msg1 = _approve_round(
        session, tmp_path, 1, "收紧超时", "  - 验收: pytest tests/test_login.py -q\n")
    assert "1 条任务已物化" in msg1, msg1
    dir2, msg2 = _approve_round(session, tmp_path, 2, "加超时提示")
    assert dir2 == dir1, "前置条件：两轮必须是同一个草稿目录，否则本条毫无判别力"
    assert "1 条任务已物化" in msg2, msg2

    contents = [t.content for t in list_tasks("s1")]
    assert len(contents) == len(set(contents)), f"第二次批准把上一轮的任务又物化了一遍: {contents}"
    assert contents == ["收紧超时", "加超时提示"], contents


def test_draft_dir_survives_when_integration_fails(ws, tmp_path):
    """收口 A 的反面：integration 失败时草稿是计划的**唯一**副本，删了就没了。

    构造的是真会发生的 slug 碰撞，不是 monkeypatch 出来的假失败——plan.md 没有
    `# 标题` 时 slug 兜底成 `plan-{session_id}`（plan_mode.py:327），与草稿目录
    同名，create_plan 于是抛「already exists」，被 plan_mode.py:359-361 吞掉返回
    None。所以「plan_result 为真」正是「计划已经另有落地副本」的判据。
    """
    session = _new_session()
    mgr = PlanModeManager(workspace=tmp_path, session_id="s1")
    plan_dir = mgr.generate_plan_dir()
    (plan_dir / "plan.md").write_text("## 任务清单\n\n- [ ] 1. 做点什么\n", encoding="utf-8")
    mgr.plan_dir = plan_dir

    msg = _finalize(_FakeAgent(session), mgr)
    assert "Proceed with implementation." in msg, msg
    assert not session.plan_slug
    assert (plan_dir / "plan.md").exists(), "integration 失败却把草稿删了——计划没了"


def test_result_message_points_at_the_plan_entry_not_the_cleared_draft(ws, tmp_path):
    """草稿目录清了以后，消息里再指它就是指一个不存在的路径。

    「已批准的计划文档冻结在 X」与 clear-and-execute 分支的 "Plan directory: X"
    都必须指向 plan 系统目录（add_artifact 真正落盘的地方），而不是已删的草稿目录。
    """
    from agents.plan.plan_manager import get_plans_dir
    session = _new_session()
    draft_dir, msg = _approve_round(session, tmp_path, 1, "收紧超时")
    assert session.plan_slug
    plan_home = get_plans_dir() / session.plan_slug
    assert str(plan_home) in msg, msg
    assert str(draft_dir) not in msg, f"消息仍指着已删的草稿目录: {msg}"
    assert plan_home.exists() and (plan_home / "tasks.md").exists()


# ────────────── 收口 B：截断提示要在 materialized==0 时也可见 ──────────────

def test_zero_materialized_still_flags_truncation(ws, tmp_path, monkeypatch):
    """`if not complete:` 那条提示此前嵌在 `if materialized:` 里面：读取/解析就抛
    （complete=False, count=0）时模型只剩 "Proceed with implementation."，而错误
    只进了 stderr——它于是会当没有计划、直接开工。
    """
    def boom(*a, **k):
        raise OSError("tasks.md unreadable")

    monkeypatch.setattr(plan_tool_executor, "get_tasks", boom)
    mgr = _draft_mgr(tmp_path, spec_md=SPEC_MD, design_md=DESIGN_MD, tasks_md=STRUCTURED)
    msg = _finalize(_FakeAgent(_new_session()), mgr)
    assert "Proceed with implementation." in msg, msg
    assert "可能不完整" in msg, msg


# ────────────── 收口 C：小节边界要认 H1 ──────────────

def test_strip_task_checklist_section_stops_at_following_h1():
    """`_NEXT_HEADING_RE` 此前是 `#{2,}`：后随的 H1（`# …`）不算小节边界，于是从
    `## 任务清单` 一直到文末全被切掉——H1 连同它后面整段散文都从对话版计划里消失。
    """
    from agents.plan.plan_tool_executor import _strip_task_checklist_section
    text = (
        "# 总标题\n\n## 背景\n\nB\n\n## 任务清单\n\n- [ ] 1. 做点什么\n\n"
        "# 下一部分\n\n这段必须活下来\n"
    )
    out = _strip_task_checklist_section(text)
    assert "- [ ]" not in out and "做点什么" not in out, out
    assert "## 背景" in out and "B" in out, out
    assert "# 下一部分" in out, out
    assert "这段必须活下来" in out, out


# ────────────── 收口 D：兜底文案不许说「计划是空的」 ──────────────

def test_checklist_only_plan_does_not_say_empty(ws, tmp_path):
    """plan.md 只有任务清单小节时 conversation_plan 为空，兜底文案此前是
    "(empty plan)"——计划不是空的，空的是它的散文正文，清单已经物化进 task_list。

    刻意**不**回退到 full_plan：那正是在「清单就是整份计划」这种情况下把清单重新
    嵌回对话，撤销它来自的那个修复。
    """
    from agents.plan.plan_tool_executor import _validate_and_load_draft
    mgr = _draft_mgr(tmp_path, plan_md="## 任务清单\n\n- [ ] 1. 做点什么\n")
    draft = _validate_and_load_draft(mgr)
    assert draft.granularity == "minimal"
    assert "- [ ]" not in draft.conversation_plan
    assert "做点什么" not in draft.conversation_plan, "不许回退到 full_plan"
    assert "(empty plan)" not in draft.conversation_plan
    assert "task_list" in draft.conversation_plan
    # 给人看的那份一个字不减，清单还在
    assert "- [ ] 1. 做点什么" in draft.full_plan


# ────────────── 收口 E：_clear_draft_dir 的守卫不许逃出 try ──────────────

def _capture_errors(monkeypatch) -> list[str]:
    """收住本模块的 print_error（它走 logger.error，capsys 抓不到）。"""
    logged: list[str] = []
    monkeypatch.setattr(plan_tool_executor, "print_error", logged.append)
    return logged


def test_clear_draft_dir_refuses_to_delete_the_plan_entry_itself(ws, monkeypatch):
    """自撞守卫钉在这里：草稿目录 resolve 后就是 plan 系统目录时**拒绝删除**并留痕。

    不是纯理论边界——slug 的兜底值恰好是 `plan-{session_id}`（plan_mode.py:327），与
    草稿目录同名。要删的是用户那份已批准计划的唯一副本，所以收口 F 把守卫挪进 try
    之后，它不能被顺手弱化成「一律吞掉照删」：吞异常与拒绝删除是两件事。
    """
    from agents.plan.plan_manager import get_plans_dir
    from agents.plan.plan_tool_executor import _clear_draft_dir

    logged = _capture_errors(monkeypatch)
    create_plan(slug=SLUG, granularity=PlanGranularity.STANDARD)
    plan_home = get_plans_dir() / SLUG
    (plan_home / "tasks.md").write_text("approved plan", encoding="utf-8")

    _clear_draft_dir(plan_home, SLUG)

    assert plan_home.exists(), "自撞时把用户唯一那份已批准计划删了"
    assert (plan_home / "tasks.md").read_text(encoding="utf-8") == "approved plan"
    assert any("refusing to clear" in m for m in logged), logged


def test_clear_draft_dir_swallows_a_failure_in_its_own_guard(ws, tmp_path, monkeypatch):
    """守卫那一行（draft.resolve() / get_plans_dir()）此前站在 try **外面**，而它下面的
    rmtree 在 try 里面。函数自陈的不变量「清理失败不能让一次已经生效的批准返回错误
    文本」于是只覆盖了后半截。调用点（plan_tool_executor.py:273）跑在
    `agent.permission_mode = target_mode`、`mgr.plan_dir = None`、
    `_emit_permission_mode_event()` 之前，守卫抛一下就把批准拦腰打断：模式没切、事件
    没发、结果消息没回。

    降级必须是三件事一起：不抛、留痕、**不删**——守卫没跑完就等于自撞与否未知，未知时
    保留草稿是唯一安全的那一边。
    """
    from agents.plan.plan_tool_executor import _clear_draft_dir

    logged = _capture_errors(monkeypatch)

    def boom():
        raise OSError("plans dir unavailable")

    monkeypatch.setattr(plan_tool_executor, "get_plans_dir", boom)
    draft_dir = tmp_path / "draft"
    draft_dir.mkdir()
    (draft_dir / "plan.md").write_text("## 任务清单\n\n- [ ] 1. 做点什么\n", encoding="utf-8")

    _clear_draft_dir(draft_dir, SLUG)          # 不得抛

    assert logged, "守卫失败必须留痕：静默不是降级，是消失"
    assert draft_dir.exists(), "自撞与否未知时不许删"
    assert (draft_dir / "plan.md").exists()


# ────────────── 收口 F：标题名缺口要可观测，不许静默 ──────────────

def _plan_md_with_checklist(heading: str) -> str:
    """轻量轨 plan.md：checkbox 清单在 `heading` 下（heading 为空 = 压根没写标题）。"""
    head = f"{heading}\n\n" if heading else ""
    return (
        "# Fix Login Timeout\n\n## 背景\n\n登录请求要等 30s 才超时。\n\n"
        f"{head}- [ ] 1. 收紧超时\n- [ ] 2. 加超时提示\n\n## 验收\n\npytest 全绿。\n"
    )


@pytest.mark.parametrize("heading", ["## Tasks", "## 任务", ""],
                         ids=["tasks-en", "renwu", "no-heading"])
def test_unrecognized_checklist_heading_is_reported(ws, tmp_path, monkeypatch, heading):
    """strip 只认 `## 任务清单`。模型写成别的名字（或不写标题）时它逐字原样返回，于是
    conversation_plan == full_plan：整份 checkbox 清单重新进对话，正是这套设计要消掉的
    那笔重复账，而且此前**无声无息**地发生。

    刻意**不**加标题名启发式：猜 `## Tasks` / `## 任务` / 全角空格，猜错就会切掉一段
    合法散文。这里只把缺口变成一条可观测的诊断，好让 later plan 拿到「到底需不需要
    名字变体规则」的真数据，而不是靠想象。
    """
    from agents.plan.plan_tool_executor import _validate_and_load_draft

    logged = _capture_errors(monkeypatch)
    traced: list[str] = []
    monkeypatch.setattr(plan_tool_executor, "trace_event",
                        lambda kind, **kw: traced.append(kind))

    mgr = _draft_mgr(tmp_path, plan_md=_plan_md_with_checklist(heading))
    draft = _validate_and_load_draft(mgr)

    # 前置条件：清单确实解析出来了，而 strip 什么都没切掉——缺口本身，不是 fixture 造的
    assert draft.granularity == "minimal"
    assert "收紧超时" in draft.tasks
    assert "- [ ] 1. 收紧超时" in draft.conversation_plan, "前置条件：清单回到对话里了"
    assert draft.conversation_plan == draft.full_plan, "前置条件：这就是那笔重复账"

    assert any("任务清单" in m for m in logged), logged
    assert traced == ["plan_mode.checklist_section_unrecognized"], traced


def test_prescribed_checklist_heading_reports_nothing(ws, tmp_path, monkeypatch):
    """反面：规定写法下 strip 生效，一条诊断都不许发。这条信号一旦被噪声淹掉，later
    plan 拿到的数据就没有判别力，等于白埋。"""
    from agents.plan.plan_tool_executor import _validate_and_load_draft

    logged = _capture_errors(monkeypatch)
    traced: list[str] = []
    monkeypatch.setattr(plan_tool_executor, "trace_event",
                        lambda kind, **kw: traced.append(kind))

    mgr = _draft_mgr(tmp_path, plan_md=MINIMAL_PLAN_MD)
    draft = _validate_and_load_draft(mgr)

    assert "- [ ]" not in draft.conversation_plan      # strip 生效了
    assert logged == [] and traced == []


def test_standard_track_never_reports_the_checklist_gap(ws, tmp_path, monkeypatch):
    """重量轨没有 `## 任务清单` 可切（清单在独立的 tasks.md 里，conversation_plan 的
    组装直接略过 `## Tasks` 段），诊断条件里的 `granularity == "minimal"` 就是为此。
    漏了这个限定，重量轨的 plan_md 是空串、`_strip("") == ""` 恒成立，于是**每次**重量轨
    批准都会喊一次假警报。"""
    from agents.plan.plan_tool_executor import _validate_and_load_draft

    logged = _capture_errors(monkeypatch)
    traced: list[str] = []
    monkeypatch.setattr(plan_tool_executor, "trace_event",
                        lambda kind, **kw: traced.append(kind))

    mgr = _draft_mgr(tmp_path, spec_md=SPEC_MD, design_md=DESIGN_MD, tasks_md=STRUCTURED)
    draft = _validate_and_load_draft(mgr)

    assert draft.granularity == "standard"
    assert "实现解析器" in draft.tasks                 # 任务照样解析出来了
    assert "## Tasks" not in draft.conversation_plan   # 重量轨的清单本来就省得掉
    assert logged == [] and traced == []
