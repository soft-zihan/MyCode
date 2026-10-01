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
from agents.tools import task_tools
from agents.tools.task_store import (
    VALID_STATUSES,
    list_tasks,
    mark_detail_disclosed,
    needs_disclosure,
)
from agents.tools.task_tools import TASK_LIST_TOOL, handle_task_list


@pytest.fixture
def ws(tmp_path):
    token = set_workspace(tmp_path)
    yield tmp_path
    reset_workspace(token)


def _call(inp: dict, seq: int | None = None, evidence=None) -> dict:
    """evidence = 验收闸门的 evidence_fn（Plan 2 Task 3）。

    刻意是关键字参数并追加在末尾：本文件 67 个调用点全都只传一个位置参数
    （inp）+ `seq=` 关键字，所以加参数不会让任何既有调用悄悄把值落进 evidence。
    None 表示闸门静默不启用（子智能体/旧调用方的形状）。
    """
    return json.loads(
        handle_task_list("s", inp, current_seq=seq, evidence_fn=evidence))


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


def test_add_rejects_non_string_detail(ws):
    """C1 的第一道防线：模型边界必须回可恢复的 Error，而不是 ok:true。

    未修时 detail=12345 原样落盘（`or ""` 只收窄 nullity 不收窄类型），此后每次
    请求推式层与常驻层都抛 AttributeError 并被两处宽 except 吞掉——两层永久静默
    关闭，而模型收到的最后一个信号是「成功」。
    """
    raw = handle_task_list("s", {"operation": "add", "content": "A", "detail": 12345})
    assert raw.startswith("Error")
    assert "detail" in raw
    assert list_tasks("s") == []          # 什么都没落盘


def test_add_rejects_non_string_acceptance(ws):
    """acceptance 是同一个类型收窄口径的第二个字段（对象是这里的典型模型错误）。"""
    raw = handle_task_list("s", {"operation": "add", "content": "A",
                                 "acceptance": {"cmd": "pytest"}})
    assert raw.startswith("Error")
    assert "acceptance" in raw
    assert list_tasks("s") == []


def test_update_rejects_non_string_content(ws):
    """update 侧同一口径：content 传数组不得污染已有条目。"""
    added = _call({"operation": "add", "content": "A", "detail": "方案A"})
    raw = handle_task_list("s", {"operation": "update", "id": added["task"]["id"],
                                 "content": ["a", "b"]})
    assert raw.startswith("Error")
    assert "content" in raw
    got = list_tasks("s")[0]
    assert got.content == "A"             # 原值未被改写
    assert got.detail == "方案A"


def test_update_rejects_blank_content(ws):
    """update 侧的空白 content 与 add 同罪（F1）：不得把一行摘要抹成空。

    常驻摘要 S 与披露块标题都用 content，空白条在两个视图里都不可读
    （`  #5   [pending]` / `## 当前任务的执行方案（#5 ）`），而此前全程零报错：
    UI 的「全选内容 → 失焦」就能造出 POST 专门防的那个状态。
    """
    added = _call({"operation": "add", "content": "A", "detail": "方案A"})
    task_id = added["task"]["id"]
    for blank in ("", "   "):
        raw = handle_task_list(
            "s", {"operation": "update", "id": task_id, "content": blank})
        assert raw.startswith("Error"), raw
        assert "content" in raw
    got = list_tasks("s")[0]
    assert got.content == "A"             # 原值未被改写
    assert got.detail == "方案A"


def test_update_without_content_key_still_updates_other_fields(ws):
    """「没给 content」≠「给了空的」：键缺席或为 null 表示不改这个字段，必须放行。

    守卫若写成 add 分支那句 `not (inp.get("content") or "").strip()`，这条会红
    ——那种写法把「只改 status」「只改 detail」也一起拒了，而 update 最常见的形状
    恰恰就是不带 content。
    """
    added = _call({"operation": "add", "content": "A", "detail": "方案A"})
    task_id = added["task"]["id"]

    out = _call({"operation": "update", "id": task_id, "status": "in_progress"})
    assert out["ok"] is True
    assert list_tasks("s")[0].status == "in_progress"

    out = _call({"operation": "update", "id": task_id, "acceptance": "pytest -k a"})
    assert out["ok"] is True
    got = list_tasks("s")[0]
    assert got.content == "A"             # 没给 content → 原值不动
    assert got.acceptance == "pytest -k a"

    # 显式 null 与键缺席同义（update_task 的 content=None 语义）
    out = _call({"operation": "update", "id": task_id, "content": None,
                 "detail": "新方案"})
    assert out["ok"] is True
    got = list_tasks("s")[0]
    assert got.content == "A"
    assert got.detail == "新方案"


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
    # priority 已删除（Plan 3a Task 1）：它没有任何读者——S 不渲染它、find_focus
    # 不用它做 tie-break，而把它变成 tie-break 会贬低列表顺序（自锚守卫存在的理由）。
    assert set(entry) == {"id", "content", "status", "acceptance", "error"}
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
    """义务 2：schema 与 store 词表一致的唯一门禁，防止再次悄悄分叉。

    priority 属性已随字段一起删除（Plan 3a Task 1），status 是 schema 里唯一
    一个由 store 词表背书的 enum。
    """
    props = TASK_LIST_TOOL["input_schema"]["properties"]
    assert set(props["status"]["enum"]) == VALID_STATUSES


def test_schema_no_longer_advertises_priority():
    """删字段必须连广告一起删：schema 里留着 priority 会让模型继续发它。"""
    assert "priority" not in TASK_LIST_TOOL["input_schema"]["properties"]
    assert "priority" not in json.dumps(TASK_LIST_TOOL, ensure_ascii=False)


def test_summary_dict_has_no_priority(ws):
    out = _call({"operation": "add", "content": "A"})
    assert "priority" not in out["task"]
    listed = _call({"operation": "list"})["tasks"][0]
    assert set(listed) == {"id", "content", "status", "acceptance", "error"}


def test_priority_is_reported_as_ignored_not_silently_dropped(ws):
    """模型仍按旧 schema 发 priority 时，必须得到明确回显而不是静默丢弃。

    `_KEYS_BY_OPERATION["add"]` 去掉 "priority" 正是为了让它落进 ignored 机制
    ——留在集合里就是「收下然后扔掉」，模型收到的仍是干净的 ok:true。
    """
    out = _call({"operation": "add", "content": "A", "priority": "high"})
    assert out["ok"] is True
    assert "priority" in out.get("ignored", [])
    assert len(list_tasks("s")) == 1                 # 任务照旧建出来了
    assert not hasattr(list_tasks("s")[0], "priority")


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
    mark_detail_disclosed("s", added["task"]["id"], 10, "旧方案")
    _call({"operation": "update", "id": added["task"]["id"],
           "detail": "人改过的新方案"}, seq=55)
    got = list_tasks("s")[0]
    assert got.detail == "人改过的新方案"
    assert got.detail_origin_seq == 55   # 重指向承载这次编辑的事件，不是清空也不是不动


def test_update_same_detail_with_seq_repoints_origin_seq(ws):
    """I-2 (a)：工具层传**相同**的 detail + 有 current_seq → 标记被重指向。

    与 HTTP 面**相反**的期望（那边相同文本时标记一动不动：
    tests/unit/test_task_endpoints.py::test_patch_same_detail_without_seq_keeps_origin_seq）。
    判别式就是 current_seq 有没有：模型自己刚写的 detail 正躺在本次 tool_calls 的参数里，
    重指向一个**可见的** seq 才能避免「旧标记指向的事件被折叠后重注入一次模型刚写过的
    内容」。评审建议的 `detail != item.detail` 一行改法对 HTTP 面是对的，但会把这条改坏。
    """
    added = _call({"operation": "add", "content": "A", "detail": "方案"})
    task_id = added["task"]["id"]
    mark_detail_disclosed("s", task_id, 10, "方案")
    assert list_tasks("s")[0].detail_origin_seq == 10

    _call({"operation": "update", "id": task_id, "detail": "方案"}, seq=55)

    got = list_tasks("s")[0]
    assert got.detail == "方案"
    assert got.detail_origin_seq == 55       # 文本没变也重指向
    assert needs_disclosure(got, [55]) is False


def test_disarmed_acceptance_gate_leaves_a_trace_only_on_the_completed_transition(ws, monkeypatch):
    """M-2：闸门**解除武装**时留痕，于是「整场没触发」与「从没被装上过」可区分。

    只钉 update→completed 这条真流转（每个任务最多走一次）：不流转、没声明
    acceptance、或闸门其实装着的时候都必须零痕迹，否则这条 trace 就成了噪声，而
    噪声正是训练人忽略它的东西。
    """
    seen: list[tuple] = []
    monkeypatch.setattr(
        task_tools, "trace_event", lambda kind, **kw: seen.append((kind, kw)))

    added = _call({"operation": "add", "content": "A", "acceptance": "pytest"})
    tid = added["task"]["id"]

    handle_task_list("s", {"operation": "update", "id": tid, "status": "completed"},
                     gate_disarmed=True)
    assert [kind for kind, _ in seen] == ["task_list.acceptance_gate_disarmed"]
    assert seen[0][1]["metadata"]["task_id"] == tid

    # 同一条再标一次 completed：不是真流转 → 不再留痕
    handle_task_list("s", {"operation": "update", "id": tid, "status": "completed"},
                     gate_disarmed=True)
    assert len(seen) == 1

    # 对照：闸门装着（gate_disarmed 缺席/False）→ 这条路上零痕迹。没有这半边，
    # 「任何 update→completed 都留痕」的坏实现也会绿。
    other = _call({"operation": "add", "content": "B", "acceptance": "pytest"})
    handle_task_list("s", {"operation": "update", "id": other["task"]["id"],
                           "status": "completed"})
    assert len(seen) == 1

    # 没声明 acceptance 的任务本来就不受闸门约束 → 也零痕迹
    plain = _call({"operation": "add", "content": "C"})
    handle_task_list("s", {"operation": "update", "id": plain["task"]["id"],
                           "status": "completed"}, gate_disarmed=True)
    assert len(seen) == 1


def test_add_with_detail_threads_current_seq_into_origin(ws):
    """I1 的接线半边：dispatcher 给 handler 的 current_seq 必须真的传进 add_task。

    store 侧的行为由 test_task_disclosure.py 的两条测试钉住；这一条钉住生产路径
    （dispatcher → handle_task_list → add_task）没有把它丢掉，否则 add(detail=...)
    仍会造出一次与 tool_calls 参数逐字相同的纯重复注入。
    """
    _call({"operation": "add", "content": "A", "detail": "方案A"}, seq=55)
    assert list_tasks("s")[0].detail_origin_seq == 55

    # 不带 seq（物化路径的调用形状）仍是 None
    _call({"operation": "add", "content": "B", "detail": "方案B"})
    assert list_tasks("s")[1].detail_origin_seq is None

    # 无 detail 时不记账：没有东西要披露，写个 seq 只会掩盖「detail 为空」这个事实
    _call({"operation": "add", "content": "C"}, seq=77)
    assert list_tasks("s")[2].detail_origin_seq is None


def test_update_without_detail_leaves_origin_seq(ws):
    """义务 5 的对照面：未改 detail 就不动 detail_origin_seq。"""
    added = _call({"operation": "add", "content": "A", "detail": "方案A"})
    mark_detail_disclosed("s", added["task"]["id"], 10, "方案A")
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


def test_task_list_is_not_granted_to_general_subagents(ws):
    """I3：子智能体拿不到常驻层，所以也不该拿到这个工具。

    prompt_runtime.build_tail_system_messages 在 _custom_system_prompt 非空时提前
    返回 []，而 subagent_runner 给**每个**子智能体都传 custom_system_prompt；但
    model_caller 的披露调用点是无条件的。于是一个 general 子智能体照工具描述建了
    清单、永远看不到摘要 S、被告诉没必要调 list，却仍要每折叠周期付最多 6000
    字符的 detail 注入；它的清单对用户也不可见（面板只取父 session id），store
    文件是孤儿。控制器裁定选项 (a)：从子智能体排除，最小且最诚实。
    """
    from agents.core.subagent import get_sub_agent_config

    names = {t["name"] for t in get_sub_agent_config("general")["tools"]}
    assert "task_list" not in names
    assert "agent" not in names          # 既有排除项没被顺手改掉
    assert "read_file" in names          # 也没有把整张工具表一起排除掉


def test_task_list_still_granted_to_main_agent():
    """I3 的对照面：排除只发生在子智能体，主智能体（Agent.tools = tool_definitions）
    照旧拿到 task_list —— 否则这条修复会把整个特性关掉。"""
    assert "task_list" in {t["name"] for t in tool_definitions}


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
    # update 半边：priority 已从 TaskItem 删除，所以模型按旧 schema 发过来的它
    # 现在对**每个**操作都是无效键，走同一条 ignored 路径。原来这里断言的
    # `list_tasks("s")[0].priority == "medium"`（丢弃后保持默认值）随字段一起
    # 消失，换成「同一次调用里的合法键照常生效」——强度不降：ignored 仍是精确
    # 相等而非子集，且多钉了一条「无效键不会连带毁掉有效键」。
    out2 = _call({"operation": "update", "id": task_id,
                  "status": "in_progress", "priority": "high"})
    assert out2["ok"] is True and out2["ignored"] == ["priority"]
    assert list_tasks("s")[0].status == "in_progress"

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

    def __init__(self, tools=None):
        # origin="sub_agent" 落进 DERIVED_SESSION_ORIGINS（session.py），会话不进
        # 用户会话列表/清理/最近会话投影。Global Constraints 要求测试会话一律用它，
        # 不用 origin="test"（不是派生 origin，会污染用户会话列表）。
        self.session = Session(session_id="s", origin="sub_agent")
        self.session_id = self.session.id
        self.current_sub_agent_id = None
        self.permission_mode = "default"
        self.last_usage_seq = -1
        # 生产 Agent 无条件有这个属性（agent.py:110），dispatcher 的验收闸门按它
        # 判断「这个 agent 有没有可能产出证据」。默认给全量工具集 = 主智能体的形状。
        self.tools = tool_definitions if tools is None else tools

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


# ---- Smoke 修复轮：detail/acceptance 空字段的工具结果软提醒 ----
#
# 冒烟证据：真实模型 14 次 task_list 调用建出 4 条任务并全部推到 completed，
# 但四条的 detail 与 acceptance 都是 0 字符 → memory_injection 0 次，推式层从未
# 触发，Plan 2 的验收闸门也因为「未声明 acceptance」而永不上膛。系统提示与工具
# description 都只是**描述**这两个字段，省略它们零代价，于是被省略。
#
# 修复口径与验收闸门一致：软提醒，不是硬闸门。硬要求会让模型为了过关编一个
# acceptance，也会与「3 步以上就建清单」的触发条件打架（简单清单常常没有可验证
# 命令）。提醒落在工具结果里而不是提示词里，因为它在调用返回后立刻被读到——那
# 正是模型还能补写的时刻。


def test_add_without_detail_or_acceptance_returns_hint(ws):
    """两个字段都空 → ok:true 照旧，但带上点名两者的 hint。"""
    out = _call({"operation": "add", "content": "A"})
    assert out["ok"] is True                      # 软提醒，不是拒绝
    assert out["action"] == "added"
    assert len(list_tasks("s")) == 1              # 任务照旧建出来了
    hint = out["hint"]
    assert "detail" in hint and "acceptance" in hint
    assert "update(" in hint                      # 给出可立即执行的补写调用
    assert f"id={out['task']['id']}" in hint      # 调用里带真实 id，模型不用自己拼


def test_add_with_detail_and_acceptance_has_no_hint(ws):
    """做对了就不啰嗦 —— hint 一旦出现就该是有信息量的。"""
    out = _call({"operation": "add", "content": "A", "detail": "方案A",
                 "acceptance": "pytest -k a"})
    assert out["ok"] is True
    assert "hint" not in out


def test_add_with_only_detail_hints_about_acceptance(ws):
    """部分填写 → 只点名剩下的那个字段（连同补写调用里也只带它）。"""
    out = _call({"operation": "add", "content": "A", "detail": "方案A"})
    hint = out["hint"]
    assert "acceptance" in hint
    assert "detail" not in hint                   # 已提供的字段不再被点名


def test_update_to_in_progress_without_detail_hints(ws):
    """转入 in_progress 时 detail 仍空 → 更短更硬的提醒，且不阻塞流转。"""
    added = _call({"operation": "add", "content": "A"})
    out = _call({"operation": "update", "id": added["task"]["id"],
                 "status": "in_progress"}, seq=10)
    assert out["ok"] is True
    assert out["task"]["status"] == "in_progress"     # 状态照旧改了
    got = list_tasks("s")[0]
    assert got.status == "in_progress"
    assert got.started_seq == 10                      # 记账也没被提醒影响
    hint = out["hint"]
    assert "detail" in hint and "update(" in hint
    assert "acceptance" not in hint                   # 一次流转只提醒一件事


def test_update_to_in_progress_with_detail_has_no_hint(ws):
    """detail 已在（或本次调用刚补上）→ 不提醒。"""
    added = _call({"operation": "add", "content": "A", "detail": "方案A"})
    out = _call({"operation": "update", "id": added["task"]["id"],
                 "status": "in_progress"}, seq=10)
    assert out["task"]["status"] == "in_progress"
    assert "hint" not in out

    # 同一次调用里既转 in_progress 又补 detail → 也不再提醒（不重复唠叨）
    added2 = _call({"operation": "add", "content": "B", "detail": "方案B"})
    out2 = _call({"operation": "update", "id": added2["task"]["id"],
                  "status": "in_progress", "detail": "改写过的方案B"}, seq=11)
    assert out2["task"]["status"] == "in_progress"
    assert "hint" not in out2
    assert list_tasks("s")[1].detail == "改写过的方案B"


def test_update_to_completed_does_not_hint(ws):
    """提醒只属于 add 与 in_progress 流转：completed / remove / list / get 都不带。"""
    added = _call({"operation": "add", "content": "A"})
    task_id = added["task"]["id"]
    _call({"operation": "update", "id": task_id, "status": "in_progress"})
    out = _call({"operation": "update", "id": task_id, "status": "completed"}, seq=20)
    assert out["ok"] is True and out["task"]["status"] == "completed"
    assert "hint" not in out                          # detail 仍空，但这里不提醒
    assert "hint" not in _call({"operation": "list"})
    assert "hint" not in _call({"operation": "get", "id": task_id})
    assert "hint" not in _call({"operation": "remove", "id": task_id})


def test_missing_operation_returns_clean_error(ws):
    """operation 键**缺席**（冒烟里 14 次调用中的那 1 次畸形调用）。

    `.get("operation", "")` 给 ""，落到末尾的 unknown-operation 分支：必须是
    点名合法操作集的 clean error，不能是 KeyError、不能是 dispatcher 宽 except
    包出来的 traceback 噪声、更不能静默 ok:true。
    """
    raw = handle_task_list("s", {})
    assert raw.startswith("Error:")
    for op in ("add", "update", "remove", "list", "get"):
        assert op in raw
    assert "KeyError" not in raw and "Traceback" not in raw
    assert list_tasks("s") == []                      # 什么都没落盘


def test_null_operation_returns_clean_error(ws):
    """operation 键**在场但为 null** —— 与缺席是两条不同的路径。

    `.get(k, default)` 在键在场时返回那个 null，默认值不生效，于是 operation
    是 None 而不是 ""。两条路必须收敛到同一句 clean error（None 会顺着
    _ignored_keys 的 `.get(None)` 拿到空集，不炸）。
    """
    raw = handle_task_list("s", {"operation": None})
    assert raw.startswith("Error:")
    for op in ("add", "update", "remove", "list", "get"):
        assert op in raw
    assert "KeyError" not in raw and "Traceback" not in raw
    assert list_tasks("s") == []


def _make_batch_stub_agent(calls: list):
    async def execute_tool_call(name, inp, assistant_seq=None):
        calls.append((name, assistant_seq))
        return ToolExecutionResult(text="ok", status="ok", outcome="success")

    return SimpleNamespace(
        session=Session(session_id="loop-stub", origin="sub_agent"),
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


# ---- Plan 2 Task 3：软验收闸门（handler 层）----
#
# 闸门本体（agents/tools/task_gate.py）在 test_task_gate.py 里按事件日志单测；
# 这里只钉 handler 的接线：什么时候上膛、什么时候静默、警告落在 JSON 的哪个位置。
# 判据是 evidence_fn 的返回值（dispatcher 传进来的、对 session.events 的闭包），
# handler 自己不看事件——store 不 import session 的规矩在这里也成立。


def test_completed_with_acceptance_and_no_evidence_warns(ws):
    added = _call({"operation": "add", "content": "跑测试",
                   "acceptance": "pytest tests/x.py -q"})
    tid = added["task"]["id"]
    _call({"operation": "update", "id": tid, "status": "in_progress"}, seq=10)
    out = _call({"operation": "update", "id": tid, "status": "completed"},
                seq=20, evidence=lambda _since: False)
    assert out["task"]["status"] == "completed"        # 警告不阻止状态变更
    assert "pytest tests/x.py -q" in out["warning"]


def test_completed_with_evidence_does_not_warn(ws):
    added = _call({"operation": "add", "content": "跑测试", "acceptance": "pytest"})
    tid = added["task"]["id"]
    out = _call({"operation": "update", "id": tid, "status": "completed"},
                seq=20, evidence=lambda _since: True)
    assert "warning" not in out


def test_completed_without_acceptance_never_warns(ws):
    """自缩放：没声明验收判据的任务不受闸门约束。"""
    added = _call({"operation": "add", "content": "写文档"})
    out = _call({"operation": "update", "id": added["task"]["id"],
                 "status": "completed"}, seq=20, evidence=lambda _since: False)
    assert "warning" not in out


def test_in_progress_transition_does_not_warn(ws):
    added = _call({"operation": "add", "content": "跑测试", "acceptance": "pytest"})
    out = _call({"operation": "update", "id": added["task"]["id"],
                 "status": "in_progress"}, seq=10, evidence=lambda _since: False)
    assert "warning" not in out


def test_no_evidence_fn_means_no_warning(ws):
    """evidence_fn 缺席（例如子智能体或旧调用方）时闸门静默不启用，不报错。"""
    added = _call({"operation": "add", "content": "跑测试", "acceptance": "pytest"})
    out = _call({"operation": "update", "id": added["task"]["id"], "status": "completed"})
    assert out["task"]["status"] == "completed"
    assert "warning" not in out


def test_warning_is_inside_the_json_not_appended(ws):
    added = _call({"operation": "add", "content": "跑测试", "acceptance": "pytest"})
    raw = handle_task_list("s", {"operation": "update", "id": added["task"]["id"],
                                 "status": "completed"},
                           current_seq=20, evidence_fn=lambda _s: False)
    json.loads(raw)      # 不抛即证明是纯 JSON


# ---- 复核轮：闸门只在**真的流转**上警告，且绝不因内部错误假指控 ----


def test_repeated_completed_update_does_not_warn_again(ws):
    """一次流转一次警告 —— 与 _start_hint 的口径同源。

    最可能的重复场景恰恰是最伤的那种：模型读到警告、认定自己确实验证过、原样再
    发一次 `status: "completed"`，于是收到逐字相同的警告。重复的噪声正是训练模型
    忽略这个机制的东西，而「误报/噪声比没有闸门更糟」是整套设计的裁定。
    """
    added = _call({"operation": "add", "content": "跑测试", "acceptance": "pytest -q"})
    tid = added["task"]["id"]
    _call({"operation": "update", "id": tid, "status": "in_progress"}, seq=10)

    first = _call({"operation": "update", "id": tid, "status": "completed"},
                  seq=20, evidence=lambda _since: False)
    assert first["task"]["status"] == "completed"
    assert "pytest -q" in first["warning"]              # 真的流转：警告

    second = _call({"operation": "update", "id": tid, "status": "completed"},
                   seq=21, evidence=lambda _since: False)
    assert second["ok"] is True
    assert second["task"]["status"] == "completed"
    assert "warning" not in second                      # 重复：不再念


def test_reopened_then_completed_again_warns_again(ws):
    """completed → in_progress → completed 是**又一次**真流转，闸门要重新上膛。

    钉住「不重复警告」没有被实现成一次性闩锁：重开一条任务再标完成是全新的验收
    时刻，那时同样可能没有证据。
    """
    added = _call({"operation": "add", "content": "跑测试", "acceptance": "pytest -q"})
    tid = added["task"]["id"]
    _call({"operation": "update", "id": tid, "status": "completed"}, seq=10,
          evidence=lambda _s: False)
    _call({"operation": "update", "id": tid, "status": "in_progress"}, seq=20)
    out = _call({"operation": "update", "id": tid, "status": "completed"}, seq=30,
                evidence=lambda _s: False)
    assert "warning" in out


def test_evidence_fn_that_raises_leaves_the_update_clean(ws, monkeypatch):
    """探针抛异常 → 更新照样成功、payload 无 warning、异常不外逃。

    失败方向是关键：**绝不能**把内部错误当成「没有证据」——那会把一次探针故障
    翻译成对模型的假指控，而假指控正是这套设计唯一禁止的结果。异常也不能外逃：
    写入发生在探针之前，外逃会让 dispatcher 的宽 except 给模型一个「其实已经
    成功了」的报错。
    """
    logged: list[str] = []
    monkeypatch.setattr(task_tools, "print_error", logged.append)
    added = _call({"operation": "add", "content": "跑测试", "acceptance": "pytest -q"})

    def boom(_since):
        raise RuntimeError("probe exploded")

    out = _call({"operation": "update", "id": added["task"]["id"],
                 "status": "completed"}, seq=20, evidence=boom)
    assert out["ok"] is True
    assert out["task"]["status"] == "completed"
    assert list_tasks("s")[0].status == "completed"      # 写入确实落盘了
    assert "warning" not in out                          # 不当成「没有证据」
    assert logged and "probe exploded" in logged[0]      # 但留痕，不静默吞


# ---- 复核轮：闸门只对**有可能产出证据**的 agent 上膛（dispatcher 层）----


async def _dispatcher_complete(tools):
    """经真 dispatcher 走一遍 add → completed（事件日志里没有任何 run_shell）。"""
    dispatcher = ToolDispatcher(agent_ref=_SeqStubAgent(tools=tools))
    await dispatcher.execute_tool_call(
        "task_list",
        {"operation": "add", "content": "跑测试", "acceptance": "pytest -q"},
        assistant_seq=7)
    res = await dispatcher.execute_tool_call(
        "task_list", {"operation": "update", "id": 1, "status": "completed"},
        assistant_seq=8)
    return json.loads(res.text)


async def test_dispatcher_without_run_shell_never_warns(ws):
    """拿不到 run_shell 的 agent 永远满足不了判据，闸门对它必须静默。

    可达路径：Plan 1 把 task_list 加进了 subagent 的 _sub_agent_excluded，但显式
    声明 `allowed-tools: task_list` 的自定义 agent 走白名单分支，而那条分支不与
    _sub_agent_excluded 求交。照样上膛就是一台保证假阳性的机器。
    刻意不按 is_sub_agent 判：有 run_shell 的子 agent，闭包绑的是它自己的 session、
    list_tasks 按 session.id 取键、子 agent 事件也落盘，闸门语义完全成立。判据是
    「能不能产出证据」，不是「它是什么」。
    """
    out = await _dispatcher_complete([TASK_LIST_TOOL])
    assert out["task"]["status"] == "completed"
    assert "warning" not in out


async def test_dispatcher_with_run_shell_still_warns(ws):
    """对照组：工具集里有 run_shell 而事件日志里没证据 → 闸门照常上膛。

    没有这条，上一条可以靠「把闸门整个关掉」trivially 通过。
    """
    shell = next(t for t in tool_definitions if t["name"] == "run_shell")
    out = await _dispatcher_complete([TASK_LIST_TOOL, shell])
    assert out["task"]["status"] == "completed"
    assert "pytest -q" in out["warning"]



# ────────────── Task 5 收口：M1 / M2 / M5 / _start_hint 转移守卫 ──────────────


def test_plan_mode_denial_message_is_one_shared_constant(ws):
    """M1：plan 模式拒绝文案此前在 dispatcher 与 permissions 里逐字重复、只差
    `Error: ` 前缀。收敛成一个模块级常量、两处引用——否则改一处忘另一处，模型在
    两条路径上会读到两种说法，而这两条路径本该是同一件事。

    dispatcher 那份很可能是不可达的纵深防御（权限门先拒），但**不删**：证明它不可达
    需要穷举所有调用路径，超出本次范围；收敛文案已经消除了「两处不同步」这个真问题。
    """
    from agents.tools import dispatcher as dispatcher_mod
    from agents.tools.permissions import PLAN_MODE_TASK_LIST_DENIAL, _check_plan_mode

    assert dispatcher_mod.PLAN_MODE_TASK_LIST_DENIAL is PLAN_MODE_TASK_LIST_DENIAL
    denied = _check_plan_mode("task_list", {}, None)
    assert denied["action"] == "deny"
    assert denied["message"] == PLAN_MODE_TASK_LIST_DENIAL
    # 文案本身仍要说清「批准后会自动物化」，否则模型不知道该怎么改
    assert "materialized into task_list automatically" in PLAN_MODE_TASK_LIST_DENIAL


async def test_dispatcher_plan_mode_denial_uses_the_same_constant(ws):
    """dispatcher 那一侧只是多了 `Error: ` 前缀，正文必须逐字相同。"""
    from agents.tools.permissions import PLAN_MODE_TASK_LIST_DENIAL

    agent = _SeqStubAgent()
    agent.permission_mode = "plan"
    res = await ToolDispatcher(agent_ref=agent).execute_tool_call(
        "task_list", {"operation": "list"}, assistant_seq=1)
    assert res.text == f"Error: {PLAN_MODE_TASK_LIST_DENIAL}"


async def test_task_list_updated_not_emitted_when_handler_errors(ws):
    """M2：handler 返回 `Error:` 时此前照样发 `task_list/updated`，于是每次被拒的
    调用都触发一次前端 refetch——而 store 根本没变。

    刻意走 handler 级的错误（未知 operation）而不是 plan 模式拒绝：后者在 dispatcher
    里 append 之前就 return 了，压根到不了发射点，用它测等于什么都没测。

    照抄 `plan/updated` 分支的既有写法（那里就有 `not result.startswith("Error")`），
    不另发明一套判据。
    """
    agent = _SeqStubAgent()
    dispatcher = ToolDispatcher(agent_ref=agent)
    res = await dispatcher.execute_tool_call(
        "task_list", {"operation": "frobnicate"}, assistant_seq=1)
    assert res.text.startswith("Error"), res.text
    emitted = [e for e in agent.session.events if e.get("type") == "task_list/updated"]
    assert emitted == [], f"被拒的调用不该触发前端 refetch: {emitted}"


async def test_task_list_updated_still_emitted_on_success(ws):
    """对照组：没有这条，上一条可以靠「把事件整个不发」trivially 通过。"""
    agent = _SeqStubAgent()
    dispatcher = ToolDispatcher(agent_ref=agent)
    res = await dispatcher.execute_tool_call(
        "task_list", {"operation": "add", "content": "A"}, assistant_seq=1)
    assert not res.text.startswith("Error"), res.text
    emitted = [e for e in agent.session.events if e.get("type") == "task_list/updated"]
    assert len(emitted) == 1


def test_add_with_unparseable_after_id_errors_explicitly(ws):
    """M5：`after_id="abc"` 此前被 _coerce_id 静默变成 None，于是一次被请求的插入
    悄悄变成「追加到末尾」、回 ok:true，而且**不进 ignored**（after_id 对 add/update
    都是合法键，_unknown_keys 认得它）。模型以为放对了位置，实际没有。
    """
    _call({"operation": "add", "content": "A"})
    _call({"operation": "add", "content": "B"})
    raw = handle_task_list("s", {"operation": "add", "content": "Z", "after_id": "abc"})
    assert raw.startswith("Error"), raw
    assert "after_id must be an integer" in raw, raw
    # 关键：那次插入**没有**悄悄发生
    assert [t.content for t in list_tasks("s")] == ["A", "B"]


def test_update_with_unparseable_after_id_errors_explicitly(ws):
    """M5 的 update 一侧：一次被请求的移动此前同样静默不发生。"""
    a = _call({"operation": "add", "content": "A"})["task"]
    _call({"operation": "add", "content": "B"})
    _call({"operation": "add", "content": "C"})
    raw = handle_task_list("s", {"operation": "update", "id": a["id"], "after_id": "abc"})
    assert raw.startswith("Error"), raw
    assert "after_id must be an integer" in raw, raw
    assert [t.content for t in list_tasks("s")] == ["A", "B", "C"]   # 没动


def test_unparseable_id_says_must_be_integer_not_required(ws):
    """M5 的 id 一侧：模型**确实发了** id，回它「id is required」/「not found」都是
    与事实矛盾的说法，模型于是会去补一个它已经给了的东西。
    """
    _call({"operation": "add", "content": "A"})
    for op in ("get", "update", "remove"):
        raw = handle_task_list("s", {"operation": op, "id": "abc"})
        assert raw.startswith("Error"), op
        assert "id must be an integer" in raw, (op, raw)
        assert "is required" not in raw and "not found" not in raw, (op, raw)


def test_absent_id_still_says_required(ws):
    """对照组：id **缺席**时仍该说 "required"——那才是事实。没有这条，上一条可以靠
    「把所有 id 错误都改说 must be an integer」trivially 通过。
    """
    _call({"operation": "add", "content": "A"})
    raw = handle_task_list("s", {"operation": "get"})
    assert "id is required" in raw, raw


def test_repeated_in_progress_does_not_re_hint(ws):
    """`_start_hint` 在重复 `status:"in_progress"` 上重复提醒 —— 与 Plan 2 给验收
    闸门修掉的是同一类噪声：警告在不该响时响，会训练模型忽略这个机制。最可能的重复
    场景恰恰最伤——模型读到提醒、去改别的字段、原样再发一次 status:"in_progress"，
    于是收到逐字相同的提醒。给它加上闸门那套 prev_status 转移守卫。
    """
    tid = _call({"operation": "add", "content": "A"})["task"]["id"]
    out1 = _call({"operation": "update", "id": tid, "status": "in_progress"}, seq=10)
    assert "hint" in out1, out1                       # 首次流转：提醒

    out2 = _call({"operation": "update", "id": tid, "status": "in_progress"}, seq=11)
    assert out2["ok"] is True and out2["task"]["status"] == "in_progress"
    assert "hint" not in out2, f"重复流转不该再提醒: {out2}"

    # 重开（回到 pending）后再转入是全新的时刻，提醒照常
    _call({"operation": "update", "id": tid, "status": "pending"}, seq=12)
    out3 = _call({"operation": "update", "id": tid, "status": "in_progress"}, seq=13)
    assert "hint" in out3, out3
