"""task_list 工具面测试。"""
from __future__ import annotations

import json

import pytest

from agents.core.workspace import reset_workspace, set_workspace
from agents.tools.task_store import (
    VALID_PRIORITIES,
    VALID_STATUSES,
    list_tasks,
    mark_detail_disclosed,
)
from agents.tools.task_tools import TASK_LIST_TOOL, handle_task_list


@pytest.fixture
def ws(tmp_path):
    token = set_workspace(tmp_path)
    yield tmp_path
    reset_workspace(token)


def _call(inp: dict, seq: int | None = None) -> dict:
    return json.loads(handle_task_list("s", inp, current_seq=seq))


def test_schema_exposes_five_operations():
    ops = TASK_LIST_TOOL["input_schema"]["properties"]["operation"]["enum"]
    assert ops == ["add", "update", "remove", "list", "get"]


def test_schema_declares_detail_acceptance_after_id_error():
    props = TASK_LIST_TOOL["input_schema"]["properties"]
    for key in ("detail", "acceptance", "after_id", "error"):
        assert key in props


def test_schema_status_enum_is_the_five_word_vocabulary():
    enum = TASK_LIST_TOOL["input_schema"]["properties"]["status"]["enum"]
    assert enum == ["pending", "in_progress", "completed", "skipped", "failed"]


def test_add_returns_created_item(ws):
    out = _call({"operation": "add", "content": "A", "detail": "方案A",
                 "acceptance": "pytest -k a"})
    assert out["ok"] is True and out["action"] == "added"
    assert out["task"]["detail"] == "方案A"
    assert out["task"]["acceptance"] == "pytest -k a"


def test_add_requires_content(ws):
    raw = handle_task_list("s", {"operation": "add"})
    assert raw.startswith("Error")


def test_add_with_after_id_inserts_in_middle(ws):
    a = _call({"operation": "add", "content": "A"})["task"]
    _call({"operation": "add", "content": "C"})
    _call({"operation": "add", "content": "B", "after_id": a["id"]})
    assert [t.content for t in list_tasks("s")] == ["A", "B", "C"]


def test_list_omits_detail(ws):
    _call({"operation": "add", "content": "A", "detail": "很长的方案",
           "acceptance": "pytest"})
    out = _call({"operation": "list"})
    assert out["count"] == 1
    entry = out["tasks"][0]
    assert "detail" not in entry
    assert entry["acceptance"] == "pytest"
    assert entry["content"] == "A"


def test_get_returns_full_detail(ws):
    added = _call({"operation": "add", "content": "A", "detail": "很长的方案"})
    out = _call({"operation": "get", "id": added["task"]["id"]})
    assert out["task"]["detail"] == "很长的方案"


def test_get_unknown_id_errors(ws):
    raw = handle_task_list("s", {"operation": "get", "id": 999})
    assert raw.startswith("Error")


def test_update_changes_status_and_detail(ws):
    added = _call({"operation": "add", "content": "A"})
    out = _call({"operation": "update", "id": added["task"]["id"],
                 "status": "in_progress", "detail": "新方案"}, seq=55)
    assert out["task"]["status"] == "in_progress"
    assert out["task"]["detail"] == "新方案"
    assert list_tasks("s")[0].started_seq == 55
    # 义务 7 的另一半：重入不覆盖 —— 回到 pending 再入 in_progress，
    # started_seq 仍锚定首次转入的那次调用（Plan 2 验收闸门靠它界定扫描区间）。
    _call({"operation": "update", "id": added["task"]["id"],
           "status": "pending"}, seq=60)
    _call({"operation": "update", "id": added["task"]["id"],
           "status": "in_progress"}, seq=61)
    assert list_tasks("s")[0].started_seq == 55


def test_update_moves_with_after_id(ws):
    a = _call({"operation": "add", "content": "A"})["task"]
    _call({"operation": "add", "content": "B"})
    c = _call({"operation": "add", "content": "C"})["task"]
    _call({"operation": "update", "id": a["id"], "after_id": c["id"]})
    assert [t.content for t in list_tasks("s")] == ["B", "C", "A"]


def test_update_rejects_invalid_status(ws):
    """非法 status 返回显式 Error（义务 3），且 store 里的任务保持原状。

    注意：brief Step 1 的原稿在这里用 _call（json.loads）解结果并断言
    out["task"]["status"] == "pending"——那与义务 3 的 Error 返回互斥
    （json.loads 对 "Error: ..." 直接抛异常），也与 brief 自己 Step 3 的
    实现矛盾。按 review 裁定（义务 3）收敛：断言 Error + store 不变。
    """
    added = _call({"operation": "add", "content": "A"})
    raw = handle_task_list("s", {"operation": "update",
                                 "id": added["task"]["id"], "status": "bogus"})
    assert raw.startswith("Error")
    assert list_tasks("s")[0].status == "pending"


def test_remove_deletes(ws):
    added = _call({"operation": "add", "content": "A"})
    out = _call({"operation": "remove", "id": added["task"]["id"]})
    assert out["action"] == "removed"
    assert list_tasks("s") == []


def test_unknown_operation_errors(ws):
    raw = handle_task_list("s", {"operation": "frobnicate"})
    assert raw.startswith("Error") and "frobnicate" in raw


def test_tool_result_does_not_carry_disclosure(ws):
    """披露不是工具层的职责 —— 单一注入路径在 ensure_focus_detail_visible（Task 9）。

    工具层若也拼一份，因为它拿不到自己那条 tool_result_msg 的 seq、无法记账
    detail_origin_seq，下一轮会被重复注入，同一段 detail 出现两次。
    """
    _call({"operation": "add", "content": "A"})
    _call({"operation": "add", "content": "B", "detail": "方案B"})
    a = list_tasks("s")[0]
    _call({"operation": "update", "id": a.id, "status": "in_progress"}, seq=10)
    raw = handle_task_list(
        "s", {"operation": "update", "id": a.id, "status": "completed"}, current_seq=20
    )
    assert "方案B" not in raw
    assert "<system-reminder>" not in raw


def test_every_operation_returns_pure_json(ws):
    """_call 用 json.loads 解结果；若任何分支拼了披露尾巴，这里会直接抛异常。"""
    added = _call({"operation": "add", "content": "A", "detail": "方案A",
                   "acceptance": "pytest"})
    assert _call({"operation": "list"})["count"] == 1
    assert _call({"operation": "get", "id": added["task"]["id"]})["task"]["detail"] == "方案A"
    assert _call({"operation": "update", "id": added["task"]["id"],
                  "status": "in_progress"}, seq=7)["task"]["status"] == "in_progress"
    assert _call({"operation": "remove", "id": added["task"]["id"]})["action"] == "removed"


def test_list_never_leaks_detail(ws):
    _call({"operation": "add", "content": "A", "detail": "很长的方案",
           "acceptance": "pytest"})
    blob = json.dumps(_call({"operation": "list"}), ensure_ascii=False)
    assert "很长的方案" not in blob
    assert "pytest" in blob  # acceptance 常驻摘要，刻意保留


# ---- 前序任务审查累积的义务（Task 1 / 3 / 4+5 / 6+7） ----


def test_schema_enums_match_store_vocabulary():
    """义务 2：schema 与 store 词表一致的唯一门禁，防止再次悄悄分叉。"""
    props = TASK_LIST_TOOL["input_schema"]["properties"]
    assert set(props["status"]["enum"]) == VALID_STATUSES
    assert set(props["priority"]["enum"]) == VALID_PRIORITIES


def test_schema_declares_id_and_after_id_as_integer():
    """义务 4（schema 侧）：id 类入参声明为 integer。"""
    props = TASK_LIST_TOOL["input_schema"]["properties"]
    assert props["id"]["type"] == "integer"
    assert props["after_id"]["type"] == "integer"


def test_handler_coerces_string_ids(ws):
    """义务 4（handler 侧）：字符串 id 必须能用（模型有时会把整数写成字符串）。"""
    added = _call({"operation": "add", "content": "A"})
    second = _call({"operation": "add", "content": "B"})
    assert _call({"operation": "get", "id": str(added["task"]["id"])})["task"]["content"] == "A"
    _call({"operation": "update", "id": str(added["task"]["id"]),
           "after_id": str(second["task"]["id"])})
    assert [t.content for t in list_tasks("s")] == ["B", "A"]


def test_invalid_status_returns_error_not_ok(ws):
    """义务 3：非法 status 显式 Error，不能静默忽略后仍报 ok: true。"""
    added = _call({"operation": "add", "content": "A"})
    raw = handle_task_list("s", {"operation": "update",
                                 "id": added["task"]["id"], "status": "bogus"})
    assert raw.startswith("Error")
    assert "bogus" in raw


def test_update_detail_repoints_origin_seq(ws):
    """义务 5：update(detail=...) 把 detail_origin_seq 重指向承载这次编辑的事件。"""
    added = _call({"operation": "add", "content": "A", "detail": "旧方案"})
    mark_detail_disclosed("s", added["task"]["id"], 10)
    _call({"operation": "update", "id": added["task"]["id"],
           "detail": "人改过的新方案"}, seq=55)
    got = list_tasks("s")[0]
    assert got.detail == "人改过的新方案"
    assert got.detail_origin_seq == 55   # 重指向承载这次编辑的事件，不是清空也不是不动


def test_update_without_detail_leaves_origin_seq(ws):
    """义务 5 的对照面：未改 detail 就不动 detail_origin_seq。"""
    added = _call({"operation": "add", "content": "A", "detail": "方案A"})
    mark_detail_disclosed("s", added["task"]["id"], 10)
    _call({"operation": "update", "id": added["task"]["id"],
           "status": "in_progress"}, seq=55)
    assert list_tasks("s")[0].detail_origin_seq == 10  # 未改 detail 就不动 seq
