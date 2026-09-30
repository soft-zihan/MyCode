"""task_list 工具 — Agent 任务追踪与渐进式披露。

Plan 模式下禁用（撰写阶段的产物要进 PlanApprovalDialog 供审批，直接写 task_list
会绕过审批闸门）。plan 批准后任务由 _finalize_plan_exit 物化进来。

三层可见性（详见 deliverables/task-list-context-analysis.md §3）：
- 常驻：清单摘要走尾部 ephemeral 通道（prompt_runtime.build_tail_system_messages）
- 推：焦点条 detail，仅当它已不在可见上下文时注入（task_disclosure）
- 拉：list 只给摘要，get 给单条完整 detail
"""

from __future__ import annotations

import json

from agents.tools.task_store import (
    TASK_PRIORITY_MEDIUM,
    TASK_STATUS_COMPLETED,
    VALID_PRIORITIES,
    VALID_STATUSES,
    TaskItem,
    add_task,
    find_focus,
    list_tasks,
    remove_task,
    update_task,
)

TASK_LIST_TOOL = {
    "name": "task_list",
    "description": (
        "管理任务清单，是多步任务的唯一进度载体。清单摘要常驻你的上下文尾部，"
        "无需反复调 list 查询。\n\n"
        "何时用：任务需要 3 步以上、或用户给了多个待办时，立即用 add 建清单。\n"
        "怎么写：content 保持一句话；把怎么做/为什么/注意事项写进 detail；"
        "把可验证的完成判据（通常一条命令）写进 acceptance。\n"
        "detail 只在该条成为焦点且已不在你上下文里时才自动注入，所以不必担心"
        "写长——但也不要为了写而写。\n"
        "声明了 acceptance 的任务，标 completed 时需要事件日志里有通过的验证命令。\n\n"
        "状态流转：pending → in_progress → completed；跳过用 skipped；失败用 failed "
        "并填 error。同一时间只应有一条 in_progress。\n"
        "绝不要在实现不完整、有未解决报错、找不到必要文件时标 completed。\n\n"
        "Plan 模式下禁用。"
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "operation": {
                "type": "string",
                "enum": ["add", "update", "remove", "list", "get"],
                "description": "操作类型",
            },
            "id": {
                "type": "integer",
                "description": "任务 ID（update/remove/get 时必需）",
            },
            "content": {
                "type": "string",
                "description": "一句话任务摘要（add 时必需，update 时可选）",
            },
            "detail": {
                "type": "string",
                "description": (
                    "详细执行方案：怎么做、为什么这样做、注意事项。"
                    "只在该条成为焦点且已不在上下文里时自动披露，"
                    "也可用 get 主动取。list 不返回此字段。"
                ),
            },
            "acceptance": {
                "type": "string",
                "description": (
                    "可验证的完成判据，通常是一条命令（如 pytest tests/unit/test_x.py）。"
                    "常驻在清单摘要里。声明了它的任务，标 completed 时需要事件日志里"
                    "有通过的验证命令。"
                ),
            },
            "status": {
                "type": "string",
                "enum": ["pending", "in_progress", "completed", "skipped", "failed"],
                "description": "任务状态（update 时可选）",
            },
            "error": {
                "type": "string",
                "description": "失败原因（status=failed 时填）",
            },
            "priority": {
                "type": "string",
                "enum": ["high", "medium", "low"],
                "description": "优先级（add 时可选，默认 medium）",
            },
            "after_id": {
                "type": "integer",
                "description": (
                    "插入/移动到该 ID 之后。0 = 放到最前。不传 = 追加末尾（add）"
                    "或不动位置（update）。用于中途加一步与重排。"
                ),
            },
        },
        "required": ["operation"],
    },
}


# 每个操作真正消费的入参（设计 §六 的操作表）。schema 是一个扁平属性袋，
# 跨操作参数（update 带 priority、add 带 status）会被静默丢弃却仍回 ok:true。
# add/update 不再回显 detail 之后，模型连「从回显里发现字段没生效」这条线索
# 都没有了，所以把被忽略的键显式报回去。
_KEYS_BY_OPERATION = {
    "add": {"operation", "content", "detail", "acceptance", "priority", "after_id"},
    "update": {"operation", "id", "content", "detail", "acceptance", "status",
               "error", "after_id"},
    "remove": {"operation", "id"},
    "list": {"operation"},
    "get": {"operation", "id"},
}


def summary_dict(item: TaskItem) -> dict:
    """摘要视图 —— 刻意不含 detail。`list` 的每条、`add`/`update` 的回显都用它。

    detail 只有一条工具出口：`get`（设计 §六）。若 add/update 也回显 detail，
    每次状态流转（pending → in_progress → completed，10 条计划约 20 次）都会把
    模型自己刚写的方案再拉回上下文一遍 —— 正是设计为 list 点名要防的「调一次
    list 就把所有方案拉进上下文」，只是换成逐条到达。而且它换不来记账正确性：
    回显落在 tool_result_msg 里，needs_disclosure 看的是 detail_origin_seq，
    那条旧事件被折叠后披露路径照样重新注入。

    error 不在设计 §六 的字段清单里，但设计另要求常驻摘要块带 failed 条的
    error 首行，且 error 只在 failed 时非空 —— 刻意保留（控制器已裁定）。
    """
    return {
        "id": item.id,
        "content": item.content,
        "status": item.status,
        "priority": item.priority,
        "acceptance": item.acceptance,
        "error": item.error,
    }


def full_dict(item: TaskItem) -> dict:
    """get 用的完整视图 —— detail 进入上下文的唯一工具出口。"""
    return item.to_dict()


def _ok(payload: dict, ignored: list[str] | None = None) -> str:
    body = {"ok": True, **payload}
    if ignored:
        body["ignored"] = ignored
    return json.dumps(body, ensure_ascii=False, indent=2)


def _ignored_keys(operation: str, inp: dict) -> list[str]:
    """本次调用里对该操作无效的键（排序返回，输出确定）。"""
    valid = _KEYS_BY_OPERATION.get(operation)
    if not valid:
        return []
    return sorted(key for key in inp if key not in valid)


def _coerce_id(value) -> int | None:
    """id 类入参统一转 int；转不动返回 None，落到既有的 clean error 分支。

    schema 声明 integer，但模型有时写字符串，所以必须转：不转的话「int
    task_id + str after_id」会绕过 store 的自锚守卫 `after_id != task_id`，
    重现「静默甩到列表末尾」。这里也不直接 int(value)：非数字（"abc"）会抛
    ValueError，被 dispatcher 的宽 except 包成 "tool 'task_list' failed:
    ValueError: ..."，模型看到的是噪声；而且行为会随清单空/非空而不同（空清单
    在循环前就返回 not found）。返回 None 让两条路收敛到同一句 clean error。
    """
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# 四个字符串字段（设计 §四）。schema 已声明 type: string，但网关不强制，所以
# 边界必须自己校验——这是 C1 的第一道防线，第二道是 task_store._as_str。
_STRING_FIELDS = ("content", "detail", "acceptance", "error")


def _check_string_fields(inp: dict) -> str | None:
    """模型边界的类型校验：本次调用里**提供了的**字符串字段必须是 str。

    为什么不能只靠 from_dict 的 _as_str：_as_str 只能把坏值收窄成 ""，模型收到的
    仍是 ok:true —— 它以为自己写下了一段执行方案，实际什么都没存，而 S 与披露层
    都不会告诉它。这里给模型一个可恢复的信号（Error: detail must be a string）。
    非字符串的 detail（步骤数组）是完全可能的模型错误，而它一旦落盘就会让
    .strip() 在每请求路径上抛 AttributeError，被 model_caller 与 prompt_runtime
    两处宽 except 吞掉：推式与常驻两层永久静默关闭，且没有 agent 可见的恢复路径。

    Plan 2 的 outcome 与 Plan 3 的写端点都会加字段——这个模式（类型校验属于模型
    边界，不只属于反序列化）从现在起就是既定的。
    """
    for key in _STRING_FIELDS:
        value = inp.get(key)
        if value is not None and not isinstance(value, str):
            return f"Error: {key} must be a string"
    return None


def handle_task_list(session_id: str, inp: dict, current_seq: int | None = None) -> str:
    """处理 task_list 工具调用。

    current_seq: 承载本次 tool_calls 的 assistant 消息的 seq，由 dispatcher
    传入（dispatcher 拿的是 agent_loop 每个模型响应捕获一次、沿工具执行链
    传下来的值，不是可变的 agent.last_usage_seq）。store 不 import session
    （会引入循环依赖并破坏可测试性），所以 seq 只能由调用方给。

    所有成功路径只返回纯 JSON，不拼披露文本：披露的单一注入路径是
    ensure_focus_detail_visible（Task 9）。工具层拿不到自己那条
    tool_result_msg 的 seq（handler 返回之后才落盘），若在这里也注入一份，
    无法记账 detail_origin_seq，下一轮会被重复注入。
    """
    operation = inp.get("operation", "")
    ignored = _ignored_keys(operation, inp)

    if operation == "add":
        type_error = _check_string_fields(inp)
        if type_error:
            return type_error
        content = (inp.get("content") or "").strip()
        if not content:
            return "Error: content is required for add operation"
        priority = inp.get("priority", TASK_PRIORITY_MEDIUM)
        if priority not in VALID_PRIORITIES:
            priority = TASK_PRIORITY_MEDIUM
        item = add_task(
            session_id,
            content,
            priority=priority,
            detail=inp.get("detail") or "",
            acceptance=inp.get("acceptance") or "",
            after_id=_coerce_id(inp.get("after_id")),
            # 承载这次 tool_calls 的 assistant seq：add(detail=...) 不记账就会在
            # 下一次模型调用时注入一份与 tool_calls 参数逐字相同的 detail（I1）。
            current_seq=current_seq,
        )
        return _ok({"action": "added", "task": summary_dict(item)}, ignored)

    if operation == "update":
        task_id = _coerce_id(inp.get("id"))
        if task_id is None:
            return "Error: id is required for update operation"
        type_error = _check_string_fields(inp)
        if type_error:
            return type_error
        status = inp.get("status")
        if status is not None and status not in VALID_STATUSES:
            return f"Error: invalid status '{status}'. Valid: {sorted(VALID_STATUSES)}"
        item = update_task(
            session_id,
            task_id,
            status=status,
            content=inp.get("content"),
            detail=inp.get("detail"),
            acceptance=inp.get("acceptance"),
            error=inp.get("error"),
            after_id=_coerce_id(inp.get("after_id")),
            current_seq=current_seq,
        )
        if item is None:
            return f"Error: task with id {task_id} not found"
        return _ok({"action": "updated", "task": summary_dict(item)}, ignored)

    if operation == "remove":
        task_id = _coerce_id(inp.get("id"))
        if task_id is None:
            return "Error: id is required for remove operation"
        if not remove_task(session_id, task_id):
            return f"Error: task with id {task_id} not found"
        return _ok({"action": "removed", "id": task_id}, ignored)

    if operation == "list":
        tasks = list_tasks(session_id)
        focus = find_focus(tasks)
        done = sum(1 for t in tasks if t.status == TASK_STATUS_COMPLETED)
        return _ok({
            "count": len(tasks),
            "completed": done,
            "focus_id": focus.id if focus else None,
            "tasks": [summary_dict(t) for t in tasks],
            "note": "摘要视图，不含 detail。用 get 取单条完整方案。",
        }, ignored)

    if operation == "get":
        task_id = _coerce_id(inp.get("id"))
        if task_id is None:
            return "Error: id is required for get operation"
        for item in list_tasks(session_id):
            if item.id == task_id:
                return _ok({"task": full_dict(item)}, ignored)
        return f"Error: task with id {task_id} not found"

    return (
        f"Error: unknown operation '{operation}'. "
        "Valid: add, update, remove, list, get"
    )
