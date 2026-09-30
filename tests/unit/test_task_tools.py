"""task_list 工具面测试。"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from agents.agent_loop import AgentLoop
from agents.core.session import Session
from agents.core.workspace import reset_workspace, set_workspace
from agents.tools.dispatcher import ToolDispatcher
from agents.tools.registry import tool_definitions
from agents.tools.result import ToolExecutionResult
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
    assert out["task"]["acceptance"] == "pytest -k a"
    # 设计 §六：只有 get 返回完整 detail。add/update 回摘要视图——模型刚写的
    # detail 正在它自己那次 tool_calls 参数里，回显是纯重复；而每次状态流转
    # 都回显一次，等于用 update 复现「一次 list 把所有方案拉进上下文」。
    assert "detail" not in out["task"]
    assert list_tasks("s")[0].detail == "方案A"   # store 里仍是全量


def test_add_requires_content(ws):
    raw = handle_task_list("s", {"operation": "add"})
    assert raw.startswith("Error")


def test_add_rejects_whitespace_only_content(ws):
    """空白 content 与缺失 content 同罪：不能建出一条摘要为空的任务。

    常驻摘要块与披露块标题都用 content，空白条在两个视图里都是不可读的。
    """
    raw = handle_task_list("s", {"operation": "add", "content": "   "})
    assert raw.startswith("Error")
    assert list_tasks("s") == []


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
    # 精确字段集，不只是 "detail" not in entry：summary_dict 现在是整个
    # 渐进式披露前提的唯一守卫，任何人往摘要里加字段都必须先改这条断言。
    # error 刻意保留（设计另要求常驻摘要块带 failed 条的 error 首行）。
    assert set(entry) == {"id", "content", "status", "priority", "acceptance", "error"}
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
    assert "detail" not in out["task"]          # update 不回显 detail（设计 §六）
    got = list_tasks("s")[0]
    assert got.detail == "新方案"                # 但 store 里已经写入
    assert got.started_seq == 55
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
    """义务 4（handler 侧）：字符串 id 必须能用（模型有时会把整数写成字符串）。

    get 半边是真靶心：不 int() 转换时 `item.id == "1"` 永不成立，会返回
    "Error: task with id 1 not found"，_call 的 json.loads 当场抛异常。
    after_id 的转换改由下面两条测试钉住——原先这里断言的 ["B", "A"] 对
    「转换」与「不转换」两种实现都成立（不转换时落到 _insert_after 的未知 id
    追加兜底，结果同样是 [B, A]），是假阴性。
    """
    added = _call({"operation": "add", "content": "A"})
    assert _call({"operation": "get", "id": str(added["task"]["id"])})["task"]["content"] == "A"
    out = _call({"operation": "update", "id": str(added["task"]["id"]),
                 "status": "in_progress"})
    assert out["task"]["status"] == "in_progress"


def test_handler_coerces_string_after_id_to_the_right_position(ws):
    """义务 4（after_id 侧，可区分版）：转换后必须真的插到锚点之后。

    三条任务 + 锚点不是最后一条，于是「插到 B 之后」与「追加到末尾」可区分：
    未转换时 _insert_after 的 `existing.id == after_id` 全部失配（2 == "2" 为
    False），落到 task_store.py 的未知 id 追加兜底 → [B, C, A]。
    """
    a = _call({"operation": "add", "content": "A"})["task"]
    b = _call({"operation": "add", "content": "B"})["task"]
    _call({"operation": "add", "content": "C"})
    _call({"operation": "update", "id": a["id"], "after_id": str(b["id"])})
    assert [t.content for t in list_tasks("s")] == ["B", "A", "C"]


def test_string_after_id_equal_to_id_is_still_a_noop(ws):
    """义务 4 的靶心：字符串自锚必须仍是 no-op（Task 5 的自锚守卫不被绕过）。

    守卫是 task_store.py 的 `after_id != task_id`。handler 不转换时
    "1" != 1 成立 → 走移动分支 → A 被静默甩到列表末尾，正是义务 4 文本里
    点名的「int task_id + str after_id 绕过自锚守卫」。转换后守卫命中，位置不动。
    """
    a = _call({"operation": "add", "content": "A"})["task"]
    _call({"operation": "add", "content": "B"})
    _call({"operation": "update", "id": a["id"], "after_id": str(a["id"])})
    assert [t.content for t in list_tasks("s")] == ["A", "B"]


def test_invalid_status_returns_error_not_ok(ws):
    """义务 3：非法 status 显式 Error，不能静默忽略后仍报 ok: true；store 保持原状。

    合并了原 test_update_rejects_invalid_status（两条测试断言的是同两件事）。
    注意：brief Step 1 的原稿在这里用 _call（json.loads）解结果并断言
    out["task"]["status"] == "pending"——那与义务 3 的 Error 返回互斥
    （json.loads 对 "Error: ..." 直接抛异常），也与 brief 自己 Step 3 的实现
    矛盾。按 review 裁定（义务 3）收敛：断言 Error + store 未被污染。
    """
    added = _call({"operation": "add", "content": "A"})
    raw = handle_task_list("s", {"operation": "update",
                                 "id": added["task"]["id"], "status": "bogus"})
    assert raw.startswith("Error")
    assert "bogus" in raw
    assert list_tasks("s")[0].status == "pending"


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


# ---- Task 8 review 修复轮 ----


def test_registry_advertises_exactly_one_task_list_schema():
    """义务 1 的回归门：registry 里只有一份 task_list，且就是 TASK_LIST_TOOL 本体。

    没有这两条断言，未来贡献者在 import 旁边再补一个字面量，两份 schema 会
    同时广告给模型 —— 正是义务 1 要终结的状态。
    """
    assert sum(t["name"] == "task_list" for t in tool_definitions) == 1
    advertised = next(t for t in tool_definitions if t["name"] == "task_list")
    assert advertised is TASK_LIST_TOOL


def test_acceptance_description_names_the_event_log_gate():
    """设计 §六 的措辞：completed 需要事件日志里有通过的验证命令。

    「需真的跑过」模型侧不可验证、也更容易靠断言蒙混；点名事件日志同时为
    Plan 2 的验收闸门预置了口径。整份 schema（含工具 description）都不该再
    出现弱版本。
    """
    props = TASK_LIST_TOOL["input_schema"]["properties"]
    desc = props["acceptance"]["description"]
    assert "事件日志" in desc and "验证命令" in desc
    assert "真的跑过" not in json.dumps(TASK_LIST_TOOL, ensure_ascii=False)


def test_cross_operation_params_are_reported_as_ignored(ws):
    """schema 是一个扁平属性袋，跨操作参数会被静默丢弃 —— 必须显式告知。

    add/update 不再回显 detail 之后（设计 §六），模型失去了「从回显里发现
    字段没生效」这条唯一的线索，所以 ok:true 必须带上 ignored 列表。
    """
    out = _call({"operation": "add", "content": "A", "status": "completed"})
    assert out["ok"] is True and out["ignored"] == ["status"]
    assert list_tasks("s")[0].status == "pending"     # 丢弃是事实，但现在有告知

    task_id = out["task"]["id"]
    out2 = _call({"operation": "update", "id": task_id,
                  "status": "in_progress", "priority": "high"})
    assert out2["ok"] is True and out2["ignored"] == ["priority"]
    assert list_tasks("s")[0].priority == "medium"

    # 没有跨操作参数时不带这个键（保持输出干净）
    assert "ignored" not in _call({"operation": "list"})
    assert "ignored" not in _call({"operation": "get", "id": task_id})


def test_non_numeric_id_returns_clean_error_not_a_traceback(ws):
    """id 解析失败要落到既有的 clean error，不能让 int() 抛 ValueError。

    未修时行为还不一致：空清单返回 "not found"，非空清单抛 ValueError →
    dispatcher 的宽 except 包成 "tool 'task_list' failed: ValueError: ..."，
    模型看到的是噪声而不是可行动的错误。
    """
    _call({"operation": "add", "content": "A"})
    for op in ("get", "update", "remove"):
        raw = handle_task_list("s", {"operation": op, "id": "abc"})
        assert raw.startswith("Error"), op
        assert "ValueError" not in raw and "Traceback" not in raw
    assert [t.content for t in list_tasks("s")] == ["A"]      # 未被误删


class _SeqStubAgent:
    """最小 agent 替身：last_usage_seq 已被同批次的 compact_context 重置成 -1。"""

    def __init__(self):
        self.session = Session(session_id="s", origin="test")
        self.session_id = self.session.id
        self.current_sub_agent_id = None
        self.permission_mode = "default"
        self.last_usage_seq = -1

    def abort_requested(self) -> bool:
        return False


async def test_dispatcher_uses_threaded_seq_not_last_usage_seq(ws):
    """Finding 1：current_seq 由调用链传入，不读 agent.last_usage_seq。

    compact_context 与 task_list 都是 sequential，模型一批
    [compact_context, task_list] 时，前者经 _compact_conversation →
    reset_context_token_estimate（agents/core/context.py:100）把
    last_usage_seq 置回 -1。若 task_list 读那个属性，started_seq 会被
    write-once 守卫永久钉成 -1（Plan 2 的验收闸门靠它界定扫描区间）。
    """
    agent = _SeqStubAgent()
    dispatcher = ToolDispatcher(agent_ref=agent)
    await dispatcher.execute_tool_call(
        "task_list", {"operation": "add", "content": "A"}, assistant_seq=42)
    await dispatcher.execute_tool_call(
        "task_list", {"operation": "update", "id": 1, "status": "in_progress"},
        assistant_seq=42)
    assert list_tasks("s")[0].started_seq == 42        # 不是 -1，也不是 None
    assert agent.last_usage_seq == -1                  # 属性本身没被偷偷改回去


async def test_dispatcher_clamps_negative_seq_to_none(ws):
    """Finding 1 的防御层：负 seq（-1 = 未知）必须夹成 None 再进 store。

    None 走已分析过的安全路径——started_seq 保持未写、后续流转仍能写入，
    detail_origin_seq=None 只多披露一次；而 -1 会被 write-once 守卫钉死。
    """
    dispatcher = ToolDispatcher(agent_ref=_SeqStubAgent())
    await dispatcher.execute_tool_call(
        "task_list", {"operation": "add", "content": "A"}, assistant_seq=-1)
    await dispatcher.execute_tool_call(
        "task_list", {"operation": "update", "id": 1, "status": "in_progress"},
        assistant_seq=-1)
    assert list_tasks("s")[0].started_seq is None
    # 之后拿到真 seq 的流转仍能补写（-1 会永久钉住，None 不会）
    await dispatcher.execute_tool_call(
        "task_list", {"operation": "update", "id": 1, "status": "in_progress"},
        assistant_seq=99)
    assert list_tasks("s")[0].started_seq == 99


def _make_batch_stub_agent(calls: list):
    async def execute_tool_call(name, inp, assistant_seq=None):
        calls.append((name, assistant_seq))
        return ToolExecutionResult(text="ok", status="ok", outcome="success")

    return SimpleNamespace(
        session=Session(session_id="loop-stub", origin="test"),
        _current_turn=1,
        _current_step=0,
        _tool_call_count=0,
        max_tool_calls=None,
        abort_requested=lambda: False,
        execute_tool_call=execute_tool_call,
        publish_tool_result_event=lambda *a, **k: None,
        record_tool_outcome=lambda *a, **k: None,
        append_tool_message=lambda *a, **k: None,
        persist_large_result=lambda fn, raw: raw,
        looks_like_tool_failure=lambda fn, text, res: False,
        context_cleared=False,
        clear_context_flag=lambda: None,
        _repeat_guard=SimpleNamespace(check=lambda fn, inp: None),
    )


async def test_both_tool_execution_paths_thread_assistant_seq(ws):
    """Finding 1：并发批与顺序批两条路径都要把 assistant_seq 送到 execute_tool_call。

    task_list 自己是 sequential，但两条路径共用同一个 execute_tool_call 契约；
    只穿一条会留下一个静默的 None（多披露一次）陷阱。read_file 是
    CONCURRENCY_SAFE_TOOLS 成员 → 走并发批，task_list → 走顺序批。
    """
    calls: list = []
    loop = AgentLoop(_make_batch_stub_agent(calls))
    loop._auto_mark_bad_case = lambda *a, **k: None

    items = [
        {"tc": {"id": "c1"}, "fn": "read_file", "inp": {"file_path": "x"}, "allowed": True},
        {"tc": {"id": "c2"}, "fn": "task_list", "inp": {"operation": "list"}, "allowed": True},
    ]
    await loop._execute_tool_batches(items, 77)
    assert calls == [("read_file", 77), ("task_list", 77)]
