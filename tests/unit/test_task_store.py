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
    get_tasks_dir,
    load_tasks,
    save_tasks,
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
