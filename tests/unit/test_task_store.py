"""task_store 单元测试。store 是纯 JSON IO，禁止 import session。"""
from __future__ import annotations

import json

import pytest

from agents.core.workspace import reset_workspace, set_workspace
from agents.tools.task_store import (
    TASK_STATUS_PENDING,
    TASK_STATUS_SKIPPED,
    VALID_STATUSES,
    TaskItem,
    TaskList,
    add_task,
    find_focus,
    get_tasks_dir,
    list_tasks,
    load_tasks,
    save_tasks,
    update_task,
)


@pytest.fixture
def ws(tmp_path):
    token = set_workspace(tmp_path)
    yield tmp_path
    reset_workspace(token)


def test_valid_statuses_is_the_five_word_vocabulary():
    assert VALID_STATUSES == {
        "pending", "in_progress", "completed", "skipped", "failed",
    }


def test_from_dict_defaults_new_fields(ws):
    item = TaskItem.from_dict({"id": 1, "content": "x"})
    assert item.detail == ""
    assert item.acceptance == ""
    assert item.error == ""
    assert item.detail_origin_seq is None
    assert item.started_seq is None


def test_from_dict_normalizes_legacy_cancelled_to_skipped(ws):
    item = TaskItem.from_dict({"id": 1, "content": "x", "status": "cancelled"})
    assert item.status == TASK_STATUS_SKIPPED


def test_from_dict_keeps_failed(ws):
    item = TaskItem.from_dict({"id": 1, "content": "x", "status": "failed"})
    assert item.status == "failed"


def test_from_dict_coerces_unknown_status_to_pending(ws):
    item = TaskItem.from_dict({"id": 1, "content": "x", "status": "bogus"})
    assert item.status == TASK_STATUS_PENDING


def test_round_trip_preserves_new_fields(ws):
    tl = TaskList(session_id="s1")
    tl.tasks.append(TaskItem(
        id=1, content="重构 X", status=TASK_STATUS_PENDING,
        detail="改 _assemble_request，注意签名不要动",
        acceptance="pytest tests/unit/test_model_caller.py",
        detail_origin_seq=42, started_seq=None, error="",
    ))
    tl.next_id = 2
    save_tasks(tl)

    got = load_tasks("s1").tasks[0]
    assert got.detail == "改 _assemble_request，注意签名不要动"
    assert got.acceptance == "pytest tests/unit/test_model_caller.py"
    assert got.detail_origin_seq == 42
    assert got.started_seq is None


def test_legacy_json_file_loads_without_migration(ws):
    path = get_tasks_dir() / "legacy.json"
    path.write_text(json.dumps({
        "session_id": "legacy",
        "todos": [{"id": 1, "content": "旧任务", "status": "cancelled",
                   "priority": "medium", "created_at": "", "updated_at": ""}],
        "next_id": 2,
    }), encoding="utf-8")

    loaded = load_tasks("legacy")
    assert len(loaded.tasks) == 1
    assert loaded.tasks[0].status == TASK_STATUS_SKIPPED
    assert loaded.tasks[0].detail == ""


def test_storage_dir_name_is_still_todos(ws):
    # 刻意保留的契约：目录名不改，避免迁移
    assert get_tasks_dir().name == "todos"


def test_save_writes_utf8_bytes_for_non_ascii_detail(ws):
    add_task("s1", "重构装配逻辑", detail="改 _assemble_request，注意签名不要动")
    raw = (get_tasks_dir() / "s1.json").read_bytes()
    assert "重构装配逻辑".encode("utf-8") in raw  # ensure_ascii=False 且真的按 UTF-8 落盘
    assert load_tasks("s1").tasks[0].detail == "改 _assemble_request，注意签名不要动"


def test_corrupt_file_is_quarantined_not_silently_emptied(ws):
    path = get_tasks_dir() / "s1.json"
    path.write_text('{"session_id": "s1", "tasks": [ TRUNCATED', encoding="utf-8")

    loaded = load_tasks("s1")
    assert loaded.tasks == []            # 工具仍可用
    assert not path.exists()             # 坏文件已被挪走
    quarantined = list(get_tasks_dir().glob("s1.corrupt-*.json"))
    assert len(quarantined) == 1
    assert "TRUNCATED" in quarantined[0].read_text(encoding="utf-8")  # 证据保留


def test_quarantine_then_save_does_not_lose_the_quarantined_copy(ws):
    path = get_tasks_dir() / "s1.json"
    path.write_text("NOT JSON AT ALL", encoding="utf-8")
    load_tasks("s1")                     # 触发隔离
    add_task("s1", "新任务")              # 随后正常写入
    assert [t.content for t in load_tasks("s1").tasks] == ["新任务"]
    assert len(list(get_tasks_dir().glob("s1.corrupt-*.json"))) == 1


def _item(i: int, status: str) -> TaskItem:
    return TaskItem(id=i, content=f"task{i}", status=status)


def test_focus_prefers_in_progress_over_earlier_pending():
    tasks = [_item(1, "pending"), _item(2, "in_progress"), _item(3, "pending")]
    assert find_focus(tasks).id == 2


def test_focus_falls_back_to_failed_over_earlier_pending():
    tasks = [_item(1, "pending"), _item(2, "failed"), _item(3, "pending")]
    assert find_focus(tasks).id == 2


def test_focus_prefers_in_progress_over_failed():
    tasks = [_item(1, "failed"), _item(2, "in_progress")]
    assert find_focus(tasks).id == 2


def test_focus_skips_completed_and_skipped():
    tasks = [_item(1, "completed"), _item(2, "skipped"), _item(3, "pending")]
    assert find_focus(tasks).id == 3


def test_focus_returns_none_when_all_terminal():
    assert find_focus([_item(1, "completed"), _item(2, "skipped")]) is None


def test_focus_returns_none_for_empty_list():
    assert find_focus([]) is None


def test_inserted_pending_does_not_steal_focus_from_in_progress():
    # 中途插一条更靠前的 pending，焦点仍锁定 in_progress
    assert find_focus([_item(9, "pending"), _item(2, "in_progress")]).id == 2


def test_out_of_order_completion_leaves_focus_on_earliest_pending():
    # #3 已完成而 #1 仍 pending：焦点是 #1，不是 #4
    tasks = [_item(1, "pending"), _item(3, "completed"), _item(4, "pending")]
    assert find_focus(tasks).id == 1


def test_focus_takes_first_in_progress_in_list_order():
    assert find_focus([_item(1, "in_progress"), _item(2, "in_progress")]).id == 1


def test_focus_takes_first_failed_in_list_order():
    assert find_focus([_item(1, "failed"), _item(2, "failed")]).id == 1


def test_add_appends_to_end_by_default(ws):
    add_task("s", "A")
    add_task("s", "B")
    assert [t.content for t in list_tasks("s")] == ["A", "B"]


def test_add_with_after_id_inserts_in_middle(ws):
    a = add_task("s", "A")
    add_task("s", "C")
    add_task("s", "B", after_id=a.id)
    assert [t.content for t in list_tasks("s")] == ["A", "B", "C"]


def test_add_with_after_id_zero_inserts_at_front(ws):
    add_task("s", "B")
    add_task("s", "A", after_id=0)
    assert [t.content for t in list_tasks("s")] == ["A", "B"]


def test_add_with_unknown_after_id_falls_back_to_append(ws):
    add_task("s", "A")
    add_task("s", "Z", after_id=999)
    assert [t.content for t in list_tasks("s")] == ["A", "Z"]


def test_update_with_after_id_moves_item(ws):
    a = add_task("s", "A")
    b = add_task("s", "B")
    c = add_task("s", "C")
    update_task("s", a.id, after_id=c.id)
    assert [t.content for t in list_tasks("s")] == ["B", "C", "A"]


def test_move_preserves_ids_and_detail(ws):
    a = add_task("s", "A", detail="detail-A", acceptance="pytest -k a")
    b = add_task("s", "B")
    update_task("s", a.id, after_id=b.id)
    moved = list_tasks("s")[1]
    assert moved.id == a.id
    assert moved.detail == "detail-A"
    assert moved.acceptance == "pytest -k a"


def test_add_ids_stay_monotonic_after_insert(ws):
    a = add_task("s", "A")
    b = add_task("s", "B")
    mid = add_task("s", "MID", after_id=a.id)
    assert mid.id == 3  # next_id 递增，与位置无关
    assert [t.id for t in list_tasks("s")] == [a.id, mid.id, b.id]


def test_update_with_self_after_id_is_a_noop(ws):
    a = add_task("s", "A")
    b = add_task("s", "B")
    add_task("s", "C")
    update_task("s", b.id, after_id=b.id)
    assert [t.content for t in list_tasks("s")] == ["A", "B", "C"]


def test_update_with_after_id_zero_moves_to_front(ws):
    a = add_task("s", "A")
    b = add_task("s", "B")
    c = add_task("s", "C")
    update_task("s", c.id, after_id=0)
    assert [t.content for t in list_tasks("s")] == ["C", "A", "B"]


def test_update_with_unknown_after_id_appends(ws):
    a = add_task("s", "A")
    add_task("s", "B")
    update_task("s", a.id, after_id=999)
    assert [t.content for t in list_tasks("s")] == ["B", "A"]


def test_update_combining_status_change_and_move_keeps_both(ws):
    a = add_task("s", "A", detail="方案A", acceptance="pytest -k a")
    add_task("s", "B")
    c = add_task("s", "C")
    update_task("s", a.id, status="in_progress", after_id=c.id)
    got = list_tasks("s")
    assert [t.content for t in got] == ["B", "C", "A"]
    assert got[2].status == "in_progress"
    assert got[2].detail == "方案A"
    assert got[2].acceptance == "pytest -k a"
