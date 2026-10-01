"""plan 批准后物化进 task_list。"""
from __future__ import annotations

import pytest

from agents.core.workspace import reset_workspace, set_workspace
from agents.plan.plan_manager import add_artifact, create_plan
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
- **函数**: parse

空输入要返回空 dict 而不是 None，下游会直接下标访问。

### Task 2: 补文档
- **状态**: [ ] pending

写 README 的用法小节。
<!-- TASKS END -->
"""


def test_materializes_every_task_in_order(ws):
    _make_plan(STRUCTURED)
    assert _materialize_plan_into_task_list("s1", SLUG) == 2
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
    assert "parse" in detail
    assert "空输入要返回空 dict" in detail


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
    _make_plan("""<!-- TASKS START -->
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
""")
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
    assert _materialize_plan_into_task_list("s1", SLUG) == 0
    assert list_tasks("s1") == []


def test_unknown_slug_returns_zero(ws):
    assert _materialize_plan_into_task_list("s1", "nope") == 0


def test_materialized_detail_reaches_the_model_via_disclosure(ws):
    """端到端：物化 → ensure_focus_detail_visible → 首条方案被注入。"""
    from agents.core.session import Session
    from agents.tools.task_disclosure import ensure_focus_detail_visible
    _make_plan(STRUCTURED)
    _materialize_plan_into_task_list("s1", SLUG)
    s = Session("s1", origin="sub_agent")
    s.system_prompt = "SYS"
    assert ensure_focus_detail_visible(s) is True
    injected = [e for e in s.events if e.get("type") == "memory_injection"]
    assert len(injected) == 1
    assert "空输入要返回空 dict" in injected[0]["content"]
    assert "实现解析器" in injected[0]["content"]
