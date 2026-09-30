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
        "声明了 acceptance 的任务，标 completed 前应当真的跑过那条命令。\n\n"
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
                    "常驻在清单摘要里。声明了它的任务，标 completed 前需真的跑过。"
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


def summary_dict(item: TaskItem) -> dict:
    """list 用的摘要视图 —— 刻意不含 detail。"""
    return {
        "id": item.id,
        "content": item.content,
        "status": item.status,
        "priority": item.priority,
        "acceptance": item.acceptance,
        "error": item.error,
    }


def full_dict(item: TaskItem) -> dict:
    """get 用的完整视图。"""
    return item.to_dict()


def _ok(payload: dict) -> str:
    return json.dumps({"ok": True, **payload}, ensure_ascii=False, indent=2)


def handle_task_list(session_id: str, inp: dict, current_seq: int | None = None) -> str:
    """处理 task_list 工具调用。

    current_seq: 承载本次 tool_calls 的 assistant 消息的 seq，由 dispatcher
    传入。store 不 import session（会引入循环依赖并破坏可测试性），所以 seq
    只能由调用方给。

    所有成功路径只返回纯 JSON，不拼披露文本：披露的单一注入路径是
    ensure_focus_detail_visible（Task 9）。工具层拿不到自己那条
    tool_result_msg 的 seq（handler 返回之后才落盘），若在这里也注入一份，
    无法记账 detail_origin_seq，下一轮会被重复注入。
    """
    operation = inp.get("operation", "")

    if operation == "add":
        content = (inp.get("content") or "").strip()
        if not content:
            return "Error: content is required for add operation"
        priority = inp.get("priority", TASK_PRIORITY_MEDIUM)
        if priority not in VALID_PRIORITIES:
            priority = TASK_PRIORITY_MEDIUM
        after_id = inp.get("after_id")
        item = add_task(
            session_id,
            content,
            priority=priority,
            detail=inp.get("detail") or "",
            acceptance=inp.get("acceptance") or "",
            after_id=int(after_id) if after_id is not None else None,
        )
        return _ok({"action": "added", "task": full_dict(item)})

    if operation == "update":
        task_id = inp.get("id")
        if task_id is None:
            return "Error: id is required for update operation"
        status = inp.get("status")
        if status is not None and status not in VALID_STATUSES:
            return f"Error: invalid status '{status}'. Valid: {sorted(VALID_STATUSES)}"
        after_id = inp.get("after_id")
        item = update_task(
            session_id,
            int(task_id),
            status=status,
            content=inp.get("content"),
            detail=inp.get("detail"),
            acceptance=inp.get("acceptance"),
            error=inp.get("error"),
            after_id=int(after_id) if after_id is not None else None,
            current_seq=current_seq,
        )
        if item is None:
            return f"Error: task with id {task_id} not found"
        return _ok({"action": "updated", "task": full_dict(item)})

    if operation == "remove":
        task_id = inp.get("id")
        if task_id is None:
            return "Error: id is required for remove operation"
        if not remove_task(session_id, int(task_id)):
            return f"Error: task with id {task_id} not found"
        return _ok({"action": "removed", "id": int(task_id)})

    if operation == "list":
        tasks = list_tasks(session_id)
        focus = find_focus(tasks)
        done = sum(1 for t in tasks if t.status == "completed")
        return _ok({
            "count": len(tasks),
            "completed": done,
            "focus_id": focus.id if focus else None,
            "tasks": [summary_dict(t) for t in tasks],
            "note": "摘要视图，不含 detail。用 get 取单条完整方案。",
        })

    if operation == "get":
        task_id = inp.get("id")
        if task_id is None:
            return "Error: id is required for get operation"
        for item in list_tasks(session_id):
            if item.id == int(task_id):
                return _ok({"task": full_dict(item)})
        return f"Error: task with id {task_id} not found"

    return (
        f"Error: unknown operation '{operation}'. "
        "Valid: add, update, remove, list, get"
    )
