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
        # origin="sub_agent" 落进 DERIVED_SESSION_ORIGINS（session.py），会话不进
        # 用户会话列表/清理/最近会话投影。Global Constraints 要求测试会话一律用它，
        # 不用 origin="test"（不是派生 origin，会污染用户会话列表）。
        self.session = Session(session_id="s", origin="sub_agent")
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
