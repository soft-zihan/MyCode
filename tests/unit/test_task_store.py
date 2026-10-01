"""task_store 单元测试。store 是纯 JSON IO，禁止 import session。"""
from __future__ import annotations

import json
import logging
import os

import pytest

from agents.core.workspace import reset_workspace, set_workspace
from agents.tools.task_store import (
    DETAIL_DISCLOSURE_CHAR_LIMIT,
    TASK_STATUS_PENDING,
    TASK_STATUS_SKIPPED,
    VALID_STATUSES,
    TaskItem,
    TaskList,
    add_task,
    clip_detail,
    find_focus,
    format_disclosure_block,
    format_task_list_block,
    get_tasks_dir,
    list_tasks,
    load_tasks,
    mark_detail_disclosed,
    needs_disclosure,
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
        # "todos" 是旧顶层键、"cancelled" 是旧状态词、"priority" 是已删字段的旧键
        # ——三者都靠 from_dict 的按名取值/归一化被吸收，无需迁移脚本。
        "todos": [{"id": 1, "content": "旧任务", "status": "cancelled",
                   "priority": "medium", "created_at": "", "updated_at": ""}],
        "next_id": 2,
    }), encoding="utf-8")

    loaded = load_tasks("legacy")
    assert len(loaded.tasks) == 1
    assert loaded.tasks[0].status == TASK_STATUS_SKIPPED
    assert loaded.tasks[0].detail == ""


def test_legacy_json_with_priority_key_still_loads(ws):
    """旧 store 文件带 priority 键，删字段后必须仍能加载（免迁移）。

    `from_dict` 按字段名取值，多余的键根本不读；`to_dict` 用 `asdict`，字段删了
    就不再输出——所以一次 re-save 后旧键自然消失，不需要迁移脚本，也不需要在
    `from_dict` 里留一条兼容分支。
    """
    path = get_tasks_dir() / "legacy.json"
    path.write_text(json.dumps({
        "session_id": "legacy",
        "tasks": [{"id": 1, "content": "旧任务", "status": "pending",
                   "priority": "high", "created_at": "", "updated_at": ""}],
        "next_id": 2,
    }), encoding="utf-8")
    loaded = load_tasks("legacy")
    assert len(loaded.tasks) == 1
    assert loaded.tasks[0].content == "旧任务"
    assert not hasattr(loaded.tasks[0], "priority")


def test_storage_dir_name_is_still_todos(ws):
    # 刻意保留的契约：目录名不改，避免迁移
    assert get_tasks_dir().name == "todos"


def test_save_writes_utf8_bytes_for_non_ascii_detail(ws):
    add_task("s1", "重构装配逻辑", detail="改 _assemble_request，注意签名不要动")
    raw = (get_tasks_dir() / "s1.json").read_bytes()
    assert "重构装配逻辑".encode("utf-8") in raw  # ensure_ascii=False 且真的按 UTF-8 落盘
    assert load_tasks("s1").tasks[0].detail == "改 _assemble_request，注意签名不要动"


def test_save_leaves_no_temp_file_behind(ws):
    """A1：save_tasks 此前是 truncate-then-write（非原子、原地）。

    本分支改变了一次撕裂写入的含义：store 现在是每请求上下文层（S 与推式披露）
    的承重件、读取翻倍、Plan 3 还要加并发 UI 写入方，所以撕裂写入意味着「模型
    静默失去它的计划」而不是「面板空了」。改为同目录临时文件 + os.replace
    （同目录才在同一文件系统上，replace 才原子）。
    """
    add_task("s1", "重构 X", detail="方案", acceptance="pytest -k x")
    update_task("s1", 1, status="in_progress")

    d = get_tasks_dir()
    assert sorted(p.name for p in d.iterdir()) == ["s1.json"]   # 无任何残留
    assert not list(d.glob("*.tmp*"))
    assert not list(d.glob(".*"))

    data = json.loads((d / "s1.json").read_text(encoding="utf-8"))
    assert data["session_id"] == "s1"
    assert [t["content"] for t in data["tasks"]] == ["重构 X"]
    assert data["tasks"][0]["detail"] == "方案"
    assert data["tasks"][0]["status"] == "in_progress"


def test_failed_save_leaves_no_temp_file_and_keeps_previous_copy(ws, monkeypatch):
    """写入本身失败时：临时文件不残留，且目标文件仍是上一份完整内容。

    这条钉住的是原子性真正买到的东西——旧实现先把目标文件截断再写，失败就留下
    一个空/半截文件，下一次 load 要么读到空清单（然后被 save 覆盖，静默丢数据），
    要么读到坏 JSON 被隔离掉。
    """
    add_task("s1", "旧任务")

    def _boom(src, dst):
        raise OSError("simulated replace failure")

    # 打在 os 模块本身上（task_store 用 `import os` + `os.replace`，所以这就是它
    # 运行时查到的那个属性）；monkeypatch 会在测试结束时还原。
    monkeypatch.setattr(os, "replace", _boom)
    with pytest.raises(OSError):
        add_task("s1", "新任务")            # 内部 save_tasks 失败

    assert sorted(p.name for p in get_tasks_dir().iterdir()) == ["s1.json"]
    assert [t.content for t in load_tasks("s1").tasks] == ["旧任务"]


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


def test_shape_corrupt_file_is_quarantined_not_left_to_raise_forever(ws):
    """I5：JSON 合法但形状非法的文件此前**根本不隔离**。

    `TaskList.from_dict(data)` 在 try 内部，而 except 元组是
    (JSONDecodeError, UnicodeDecodeError, OSError)，`{"tasks": "x"}` 抛的是
    AttributeError —— 不在元组里，于是文件永远坏着、每请求重抛，并让
    `GET /api/tasks/{id}` 永久 500（隔离本来是「保留证据 + 工具仍可用」，
    这条路两样都没有）。
    """
    path = get_tasks_dir() / "s1.json"
    path.write_text('{"session_id": "s1", "tasks": "not a list"}', encoding="utf-8")

    assert load_tasks("s1").tasks == []          # 返回空清单而不是抛
    assert not path.exists()                     # 原文件已挪走
    quarantined = list(get_tasks_dir().glob("s1.corrupt-*.json"))
    assert len(quarantined) == 1
    assert "not a list" in quarantined[0].read_text(encoding="utf-8")   # 证据保留

    add_task("s1", "新任务")                      # 随后仍可正常写入
    assert [t.content for t in load_tasks("s1").tasks] == ["新任务"]


def test_shape_corrupt_variants_are_all_quarantined(ws):
    """形状损坏不止「tasks 是字符串」一种：列表里塞标量、JSON 顶层是数组、
    status 是不可哈希的对象，走的都必须是同一条隔离路径。"""
    for name, payload in (
        ("scalar-item", '{"session_id": "s", "tasks": [1, 2]}'),
        ("top-level-list", '[1, 2, 3]'),
        ("unhashable-status", '{"session_id": "s", "tasks": [{"id": 1, "status": {}}]}'),
    ):
        session_id = f"shape-{name}"
        path = get_tasks_dir() / f"{session_id}.json"
        path.write_text(payload, encoding="utf-8")
        assert load_tasks(session_id).tasks == [], name
        assert not path.exists(), name
        assert len(list(get_tasks_dir().glob(f"{session_id}.corrupt-*.json"))) == 1, name


def test_quarantine_is_logged_naming_both_paths(ws, caplog):
    """I5：隔离改动了用户的文件系统却一行日志都没有（本模块此前不 import 任何
    日志模块）——两层同时消失，唯一证据是一个要人手工去找的重命名文件。"""
    d = get_tasks_dir()
    (d / "parse.json").write_text("NOT JSON AT ALL", encoding="utf-8")
    (d / "shape.json").write_text('{"tasks": "not a list"}', encoding="utf-8")

    with caplog.at_level(logging.ERROR, logger="mycode"):
        load_tasks("parse")
        load_tasks("shape")

    msgs = [r.getMessage() for r in caplog.records if "[task_store]" in r.getMessage()]
    assert len(msgs) == 2
    for msg in msgs:
        assert ".json" in msg and ".corrupt-" in msg      # 原路径与隔离路径都在
    assert any("parse.json" in m for m in msgs)
    assert any("shape.json" in m for m in msgs)
    # 两类原因必须可区分，否则 operator 分不清是文件被截断还是写入方写错了形状
    assert "shape" in msgs[1].lower()
    assert "shape" not in msgs[0].lower()


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
    """M11：两条以上才有判别力。

    单条列表里「追加到末尾」与「插到第一条之后」是同一个结果，于是一个退化成
    `tasks.insert(1, item)` 的实现也能过这条断言。
    """
    add_task("s", "A")
    add_task("s", "B")
    add_task("s", "Z", after_id=999)
    assert [t.content for t in list_tasks("s")] == ["A", "B", "Z"]


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


def test_no_disclosure_when_item_is_none():
    assert needs_disclosure(None, [1, 2, 3]) is False


def test_no_disclosure_when_detail_empty():
    item = TaskItem(id=1, content="x", detail="", detail_origin_seq=None)
    assert needs_disclosure(item, [1, 2]) is False


def test_no_disclosure_when_detail_is_whitespace_only():
    item = TaskItem(id=1, content="x", detail="   \n ", detail_origin_seq=None)
    assert needs_disclosure(item, [1, 2]) is False


def test_disclosure_when_never_injected():
    item = TaskItem(id=1, content="x", detail="方案", detail_origin_seq=None)
    assert needs_disclosure(item, [1, 2]) is True


def test_no_disclosure_when_origin_seq_still_visible():
    item = TaskItem(id=1, content="x", detail="方案", detail_origin_seq=7)
    assert needs_disclosure(item, [3, 7, 9]) is False


def test_disclosure_when_origin_seq_was_folded_away():
    item = TaskItem(id=1, content="x", detail="方案", detail_origin_seq=7)
    assert needs_disclosure(item, [3, 9]) is True


def test_disclosure_after_context_clear_empty_visible():
    item = TaskItem(id=1, content="x", detail="方案", detail_origin_seq=7)
    assert needs_disclosure(item, []) is True


def test_needs_disclosure_accepts_a_tuple():
    item = TaskItem(id=1, content="x", detail="方案", detail_origin_seq=7)
    assert needs_disclosure(item, (3, 7, 9)) is False
    assert needs_disclosure(item, (3, 9)) is True


def test_clip_detail_under_limit_is_unchanged():
    assert clip_detail("短方案") == "短方案"


def test_clip_detail_over_limit_is_truncated_with_marker():
    clipped = clip_detail("x" * (DETAIL_DISCLOSURE_CHAR_LIMIT + 500))
    assert len(clipped) < DETAIL_DISCLOSURE_CHAR_LIMIT + 200
    assert "截断" in clipped
    assert clipped.startswith("x" * DETAIL_DISCLOSURE_CHAR_LIMIT)


def test_clip_detail_boundary_at_exactly_limit_and_one_over():
    exact = "x" * DETAIL_DISCLOSURE_CHAR_LIMIT
    assert clip_detail(exact) == exact        # 恰好等于上限：原样返回，不加标记
    over = clip_detail(exact + "y")
    assert over.startswith(exact)             # 6000 字符全部保留
    assert "截断" in over


def test_mark_detail_disclosed_persists_seq(ws):
    item = add_task("s", "A", detail="方案A")
    assert item.detail_origin_seq is None
    mark_detail_disclosed("s", item.id, 99)
    assert list_tasks("s")[0].detail_origin_seq == 99


def test_mark_detail_disclosed_preserves_detail_text(ws):
    item = add_task("s", "A", detail="方案A", acceptance="pytest")
    mark_detail_disclosed("s", item.id, 99)
    got = list_tasks("s")[0]
    assert got.detail == "方案A"
    assert got.acceptance == "pytest"


def test_mark_detail_disclosed_unknown_id_is_noop(ws):
    add_task("s", "A")
    mark_detail_disclosed("s", 999, 1)  # 不抛异常
    assert list_tasks("s")[0].detail_origin_seq is None


def test_disclosure_block_contains_id_content_acceptance_and_detail():
    item = TaskItem(id=3, content="重构 X", detail="改 _assemble_request",
                    acceptance="pytest tests/unit/test_model_caller.py")
    block = format_disclosure_block(item)
    assert "<system-reminder>" in block and "</system-reminder>" in block
    assert "#3" in block
    assert "重构 X" in block
    assert "改 _assemble_request" in block
    assert "pytest tests/unit/test_model_caller.py" in block


def test_disclosure_block_omits_acceptance_line_when_empty():
    item = TaskItem(id=1, content="写文档", detail="步骤一二三", acceptance="")
    block = format_disclosure_block(item)
    assert "验收:" not in block
    assert "步骤一二三" in block


def test_disclosure_block_shows_error_for_failed_item():
    item = TaskItem(id=2, content="跑测试", detail="细节", status="failed",
                    error="AssertionError: 3 != 4")
    block = format_disclosure_block(item)
    assert "AssertionError: 3 != 4" in block


def test_disclosure_block_omits_error_line_when_status_is_not_failed():
    item = TaskItem(id=1, content="x", detail="方案", status="pending",
                    error="上一轮留下的陈旧错误")
    assert "上一轮留下的陈旧错误" not in format_disclosure_block(item)


def test_disclosure_block_caps_long_error_text():
    item = TaskItem(id=1, content="x", detail="方案", status="failed",
                    error="E" * 5000)
    block = format_disclosure_block(item)
    assert "E" * 500 in block
    assert "E" * 501 not in block


def test_disclosure_block_empty_when_no_detail():
    assert format_disclosure_block(TaskItem(id=1, content="x", detail="")) == ""


def test_disclosure_block_empty_for_whitespace_only_detail():
    assert format_disclosure_block(TaskItem(id=1, content="x", detail="  \n ")) == ""


def test_disclosure_block_clips_long_detail():
    item = TaskItem(id=1, content="x", detail="y" * 9000)
    block = format_disclosure_block(item)
    assert "已截断" in block


def test_disclosure_block_caps_long_content():
    """I4：content 此前**无上限、无换行归一化**地插进标题，§五 的 6000 字符上限
    于是可被轻易击穿（实测：10 万字符 content + 10 字符 detail → 100,113 字符的
    注入块，是上限的 16.7 倍）。三个插值字段一律走 _first_line 之后，块的总体积
    只由 DETAIL_DISCLOSURE_CHAR_LIMIT 与几个有界的单行字段决定。
    """
    item = TaskItem(id=1, content="C" * 100_000, detail="短方案",
                    acceptance="A" * 5000, status="failed", error="E" * 5000)
    block = format_disclosure_block(item)
    # 实测：本条 item 的块是 1,207 字符；detail 顶到 6000 上限时约 7.3k。取
    # 10_000 作为上界——覆盖最坏 detail 仍有余量，又远低于未修时同样输入的
    # 100,113（content 无界时块的体积由 content 决定，与 detail 上限无关）。
    assert len(block) < 10_000
    assert "C" * 80 in block and "C" * 81 not in block      # _S_CONTENT_CHAR_LIMIT
    assert "A" * 500 in block and "A" * 501 not in block    # _DISCLOSURE_LINE_CHAR_LIMIT
    assert "E" * 500 in block and "E" * 501 not in block
    header = block.splitlines()[1]
    assert header.startswith("## 当前任务的执行方案（#1 ")
    assert "\n" not in header


def test_disclosure_block_single_lines_multiline_fields():
    """I4 的另一半：多行字段会把块的「一个字段一行」结构撑散。

    未修时一条三行的 acceptance 会渲染成三行，模型再也无法按行定位字段（S 早已有
    这条约束，披露块没有——两套写法该收敛的正是这个语义）。
    """
    item = TaskItem(id=1, content="第一行\n第二行", detail="方案",
                    acceptance="pytest -k a\npytest -k b\npytest -k c")
    block = format_disclosure_block(item)
    assert "第二行" not in block
    assert "pytest -k b" not in block
    assert sum(1 for line in block.splitlines()
               if line.startswith("验收: ")) == 1


def test_disclosure_block_tells_model_to_mark_in_progress():
    item = TaskItem(id=5, content="部署", detail="先跑迁移")
    block = format_disclosure_block(item)
    assert "in_progress" in block
    assert (
        "（自动披露。开始执行时把 #5 标为 in_progress；"
        "完成前需有通过的验收命令。）"
    ) in block
    assert "已不在你的可见上下文中" not in block


def test_from_dict_coerces_null_strings_to_empty(ws):
    """义务 6：显式 JSON null 不得变成 None —— needs_disclosure /
    format_disclosure_block 会对这些字段调 .strip()，None 会在每模型请求
    的路径上抛 AttributeError。"""
    item = TaskItem.from_dict({"id": 1, "content": "x", "detail": None,
                               "acceptance": None, "error": None})
    assert item.detail == ""
    assert item.acceptance == ""
    assert item.error == ""


def test_from_dict_coerces_null_content_to_empty(ws):
    """content 是第四个字符串字段，收窄口径必须与 detail/acceptance/error 一致。

    `content: null` 会让 format_task_list_block 里的 _first_line 抛
    AttributeError，被 prompt_runtime 的 try/except 吞掉——整个常驻层就每请求
    静默消失。在反序列化边界收窄，对每个消费方都生效，而不只是 S。
    """
    item = TaskItem.from_dict({"id": 1, "content": None})
    assert item.content == ""


def test_from_dict_coerces_non_string_fields_to_empty(ws):
    """C1：`or ""` 只收窄 nullity，不收窄类型 —— 这里补上类型这一维。

    非字符串的 detail/acceptance（模型完全可能给 detail 传一个步骤数组、给
    acceptance 传一个对象；网关不强制 schema 类型）会原样落盘、重载后类型仍在，
    此后**每次**模型请求：推式层 needs_disclosure → item.detail.strip() 抛
    AttributeError 被 model_caller 的宽 except 吞掉，常驻层 _first_line 的 .strip()
    同样被 prompt_runtime 吞掉（实测 `tails returned: 1 | any Task List block?
    False`）。失败是黏性的、对模型静默的（工具早回过 ok:true）、且自我遮掩的
    （本该显示清单的 S 正是坏掉的那个）。

    这是最后一道防线，第一道在 task_tools 的模型边界（见 test_task_tools.py）。
    """
    for bad in (12345, {"cmd": "pytest"}, ["步骤一", "步骤二"]):
        item = TaskItem.from_dict({
            "id": 1,
            "content": bad,
            "detail": bad,
            "acceptance": bad,
            "error": bad,
        })
        assert item.content == "", bad
        assert item.detail == "", bad
        assert item.acceptance == "", bad
        assert item.error == "", bad


# --- id 的整数收窄（_as_int）-------------------------------------------------
#
# from_dict 此前收窄了四个字符串字段（_as_str）却把 id 原样透传。Plan 3b 会加 UI
# 写端点，届时 id 可能从 JSON 请求体进来；一个字符串 id 会静默让
# mark_detail_disclosed 的 `item.id == task_id` 匹配失败，于是每次调用都重注入
# 最多 6000 字符——与 C1 同一类「静默、黏性、对模型不可见」的缺陷。


def test_from_dict_coerces_non_int_id_to_zero(ws):
    item = TaskItem.from_dict({"id": "7", "content": "x"})
    assert item.id == 0            # 不是 7，也不是 "7"


def test_from_dict_coerces_bool_id_to_zero(ws):
    # bool 是 int 的子类；不排除的话 True 会变成 1 从而匹配到 id=1 的真实任务
    assert TaskItem.from_dict({"id": True, "content": "x"}).id == 0
    assert TaskItem.from_dict({"id": False, "content": "x"}).id == 0


def test_from_dict_keeps_valid_int_id(ws):
    assert TaskItem.from_dict({"id": 7, "content": "x"}).id == 7


def test_from_dict_coerces_missing_and_null_id_to_zero(ws):
    assert TaskItem.from_dict({"content": "x"}).id == 0
    assert TaskItem.from_dict({"id": None, "content": "x"}).id == 0


def test_string_id_does_not_receive_detail_disclosure_bookkeeping(ws):
    """靶心：字符串 id 让 mark_detail_disclosed 静默失配，披露于是每次都重来。"""
    add_task("s", "A", detail="方案A")
    path = get_tasks_dir() / "s.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["tasks"][0]["id"] = "1"          # 模拟 UI 写端点/旧文件送来的字符串 id
    path.write_text(json.dumps(data), encoding="utf-8")

    item = load_tasks("s").tasks[0]
    assert item.id == 0
    mark_detail_disclosed("s", item.id, 99)
    # 收窄后 id 恒为 int，记账命中同一条目，不会「每次都重注入」
    assert load_tasks("s").tasks[0].detail_origin_seq == 99


# --- 清单摘要块 S（常驻尾部通道内容）---------------------------------------
#
# S 是每请求都要重发一次的常驻块，所以这两组性质是本节的重点：
# (1) 绝不含 detail —— 含了就等于把「全量方案常驻」换皮保留，渐进式披露作废；
# (2) 每个单行字段都限长，且用的是 **S 自己的**上限（content 80 / acceptance 160
#     / error 200），不是一次性披露块的 500 —— 常驻块的预算必须按「每请求都付」
#     来定，否则 (1) 只是字段级承诺：模型可以把自由文本停在 acceptance 里让它常驻。


def test_s_block_empty_for_no_tasks():
    assert format_task_list_block([], None) == ""


def test_s_block_shows_done_count_not_done_items():
    tasks = [
        _item(1, "completed"), _item(2, "completed"), _item(3, "pending"),
    ]
    block = format_task_list_block(tasks, tasks[2])
    assert "2/3" in block
    assert "task1" not in block
    assert "task2" not in block
    assert "task3" in block


def test_s_block_marks_focus():
    tasks = [_item(1, "pending"), _item(2, "in_progress")]
    block = format_task_list_block(tasks, tasks[1])
    focus_line = next(line for line in block.splitlines() if "#2" in line)
    other_line = next(line for line in block.splitlines() if "#1" in line)
    assert focus_line.lstrip().startswith(">")
    assert not other_line.lstrip().startswith(">")


def test_s_block_includes_acceptance():
    item = TaskItem(id=1, content="跑测试", status="pending",
                    acceptance="pytest tests/unit -q")
    block = format_task_list_block([item], item)
    assert "pytest tests/unit -q" in block
    # acceptance 是模型自填的自由文本，常驻块里必须限长。用的是 S 自己的上限
    # （160），不是披露块的 500：S 每请求重发，500 会让模型能把 500 字符自由
    # 文本停在 acceptance 里变成常驻成本，「S 不含 detail」就退化成字段级承诺。
    long_item = TaskItem(id=1, content="跑测试", status="pending",
                         acceptance="A" * 5000)
    long_block = format_task_list_block([long_item], long_item)
    assert "A" * 160 in long_block
    assert "A" * 161 not in long_block


def test_s_block_never_includes_detail():
    item = TaskItem(id=1, content="跑测试", status="pending",
                    detail="一大段详细方案不该出现在常驻块里")
    block = format_task_list_block([item], item)
    assert "跑测试" in block                        # 先证明 S 真的渲染出来了
    assert "一大段详细方案" not in block             # 再证明它不含 detail


def test_s_block_shows_error_first_line_for_failed():
    item = TaskItem(id=1, content="跑测试", status="failed",
                    error="AssertionError: 3 != 4\nTraceback ...")
    block = format_task_list_block([item], item)
    assert "AssertionError: 3 != 4" in block
    assert "Traceback" not in block
    # error 同样限长：先取首行，再截到 S 自己的上限（200，只需认得出是哪个失败；
    # 披露块的 500 是一次性注入的预算，不适用于每请求重发的常驻块）
    long_item = TaskItem(id=1, content="跑测试", status="failed",
                         error="E" * 5000)
    long_block = format_task_list_block([long_item], long_item)
    assert "E" * 200 in long_block
    assert "E" * 201 not in long_block


def test_s_block_lists_skipped_count_separately():
    tasks = [_item(1, "skipped"), _item(2, "pending")]
    block = format_task_list_block(tasks, tasks[1])
    assert "skipped" in block.lower() or "跳过" in block
    assert "task1" not in block


def test_s_block_shows_status_for_each_listed_item():
    tasks = [_item(1, "in_progress"), _item(2, "pending")]
    block = format_task_list_block(tasks, tasks[0])
    assert "in_progress" in block
    assert "pending" in block


def test_s_block_truncates_long_content_to_one_line():
    item = TaskItem(id=1, content="第一行\n第二行", status="pending")
    block = format_task_list_block([item], item)
    assert "第二行" not in block
    # content 是「一句话摘要」，比 acceptance/error 更短的上限
    long_item = TaskItem(id=1, content="C" * 500, status="pending")
    long_block = format_task_list_block([long_item], long_item)
    assert "C" * 80 in long_block
    assert "C" * 81 not in long_block


def test_s_block_all_skipped_footer_does_not_claim_all_complete():
    """listed 为空不止「全部完成」一种成因。

    全 skipped 时 header 写着 `(0/3 done, 3 skipped)`，footer 再说「全部完成」
    就是自相矛盾的事实错误——而 S 是模型判断计划状态的常驻依据。footer 因此
    按 `done == len(tasks)` 分档：真全完成才说「全部完成」，否则只陈述可核的
    事实「无未完成条目」。
    """
    tasks = [_item(1, "skipped"), _item(2, "skipped"), _item(3, "skipped")]
    block = format_task_list_block(tasks, None)
    assert "(0/3 done, 3 skipped)" in block
    assert "全部完成" not in block
    assert "（无未完成条目）" in block

    # 反向：真全部完成时仍然说「全部完成」，别把两档写反
    done_tasks = [_item(1, "completed"), _item(2, "completed")]
    done_block = format_task_list_block(done_tasks, None)
    assert "(2/2 done)" in done_block
    assert "（全部完成）" in done_block


# --- 截断标记（_first_line）--------------------------------------------------
#
# 静默截断在验收闸门上线之前必须先变成可见事实：闸门会告诉模型去**执行**它读到
# 的那条 acceptance，而一条超过上限的 acceptance 此前被从中间切断且毫无提示——
# 模型会把半条命令当完整命令跑。标记让「这里被切过」成为块里可读的事实。
#
# `…` 会让返回值比 limit 多 1 个字符，这是刻意的：limit 约束的是**保留的正文字符
# 数**，标记不是正文（与 clip_detail 同一取舍）。所以下面各条既有边界断言
# （"A"*160 in / "A"*161 not in）仍然成立——`…` 不是 A/E/C。


def test_first_line_appends_ellipsis_only_when_truncated():
    from agents.tools.task_store import _first_line
    assert _first_line("短命令", 80) == "短命令"          # 未截断：逐字不变、无标记
    assert _first_line("x" * 80, 80) == "x" * 80          # 恰好等于上限：不截断
    assert _first_line("x" * 81, 80) == "x" * 80 + "…"    # 超一个字符：截断并标记


def test_first_line_takes_first_line_before_capping():
    from agents.tools.task_store import _first_line
    # 丢掉后续行也是截断，所以首行同样带 `…`（见 test_first_line_marks_vertical_truncation）
    assert _first_line("第一行\n第二行", 80) == "第一行…"


def test_first_line_marks_vertical_truncation():
    """丢行与超长是同一个事实的两种形状：「这里被切过」都必须可读。

    多行 acceptance 此前渲染成光秃秃的首行、没有任何标记，而下一个任务上线的
    验收闸门正是叫模型去**执行**它在 S 里读到的那条 acceptance——半条命令会被
    当完整命令跑。tests/unit/test_task_store.py 里早就有这样的输入（三行
    acceptance），所以这不是假想场景。
    """
    from agents.tools.task_store import _first_line
    assert _first_line("pytest -k a\npytest -k b", 80) == "pytest -k a…"
    assert _first_line("单行", 80) == "单行"          # 未截断仍逐字不变


def test_s_block_marks_truncated_acceptance():
    item = TaskItem(id=1, content="跑测试", status="pending", acceptance="A" * 300)
    block = format_task_list_block([item], item)
    assert "…" in block
    assert "A" * 160 in block and "A" * 161 not in block


def test_s_block_leaves_short_acceptance_unmarked():
    item = TaskItem(id=1, content="跑测试", status="pending",
                    acceptance="pytest tests/unit -q")
    assert "…" not in format_task_list_block([item], item)


def test_s_block_marks_multiline_acceptance():
    """S 里一条多行 acceptance 不许渲染成「看起来完整」的单行命令。"""
    item = TaskItem(id=1, content="跑测试", status="pending",
                    acceptance="pytest -k a\npytest -k b\npytest -k c")
    block = format_task_list_block([item], item)
    assert "pytest -k a…" in block
    assert "pytest -k b" not in block


def test_disclosure_block_marks_truncated_content():
    item = TaskItem(id=1, content="C" * 300, detail="方案")
    header = next(line for line in format_disclosure_block(item).splitlines() if "#1" in line)
    assert "…" in header
    assert "C" * 80 in header and "C" * 81 not in header
