"""task_list 工具 — Agent 任务追踪。

Plan 模式下禁用。
"""

from __future__ import annotations

import json
from typing import Any

from agents.tools.task_store import (
    add_todo,
    update_todo,
    remove_todo,
    list_todos,
    VALID_STATUSES,
    VALID_PRIORITIES,
    TODO_PRIORITY_MEDIUM,
)


TODOLIST_TOOL = {
    "name": "task_list",
    "description": "管理任务清单。用于追踪多步任务的进度。Plan 模式下禁用。",
    "input_schema": {
        "type": "object",
        "properties": {
            "operation": {
                "type": "string",
                "enum": ["add", "update", "remove", "list"],
                "description": "操作类型",
            },
            "id": {
                "type": "integer",
                "description": "任务 ID（update/remove 时必需）",
            },
            "content": {
                "type": "string",
                "description": "任务内容（add 时必需，update 时可选）",
            },
            "status": {
                "type": "string",
                "enum": ["pending", "in_progress", "completed", "cancelled"],
                "description": "任务状态（update 时可选）",
            },
            "priority": {
                "type": "string",
                "enum": ["high", "medium", "low"],
                "description": "任务优先级（add 时可选，默认 medium）",
            },
        },
        "required": ["operation"],
    },
}


def handle_todolist(session_id: str, inp: dict) -> str:
    """处理 task_list 工具调用。"""
    operation = inp.get("operation", "")
    
    if operation == "add":
        content = inp.get("content", "")
        if not content:
            return "Error: content is required for add operation"
        priority = inp.get("priority", TODO_PRIORITY_MEDIUM)
        item = add_todo(session_id, content, priority)
        return json.dumps({
            "ok": True,
            "action": "added",
            "todo": item.to_dict(),
        }, ensure_ascii=False, indent=2)
    
    if operation == "update":
        todo_id = inp.get("id")
        if todo_id is None:
            return "Error: id is required for update operation"
        status = inp.get("status")
        content = inp.get("content")
        item = update_todo(session_id, todo_id, status=status, content=content)
        if item is None:
            return f"Error: todo with id {todo_id} not found"
        return json.dumps({
            "ok": True,
            "action": "updated",
            "todo": item.to_dict(),
        }, ensure_ascii=False, indent=2)
    
    if operation == "remove":
        todo_id = inp.get("id")
        if todo_id is None:
            return "Error: id is required for remove operation"
        removed = remove_todo(session_id, todo_id)
        if not removed:
            return f"Error: todo with id {todo_id} not found"
        return json.dumps({
            "ok": True,
            "action": "removed",
            "id": todo_id,
        }, ensure_ascii=False, indent=2)
    
    if operation == "list":
        items = list_todos(session_id)
        return json.dumps({
            "ok": True,
            "count": len(items),
            "todos": [item.to_dict() for item in items],
        }, ensure_ascii=False, indent=2)
    
    return f"Error: unknown operation '{operation}'. Valid: add, update, remove, list"
