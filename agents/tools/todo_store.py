"""TodoList 存储 — JSON 文件持久化。

存储在 ~/.mycode/todos/{session_id}.json
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

from agents.core.workspace import get_workspace


TODO_STATUS_PENDING = "pending"
TODO_STATUS_IN_PROGRESS = "in_progress"
TODO_STATUS_COMPLETED = "completed"
TODO_STATUS_CANCELLED = "cancelled"

VALID_STATUSES = {TODO_STATUS_PENDING, TODO_STATUS_IN_PROGRESS, TODO_STATUS_COMPLETED, TODO_STATUS_CANCELLED}

TODO_PRIORITY_HIGH = "high"
TODO_PRIORITY_MEDIUM = "medium"
TODO_PRIORITY_LOW = "low"

VALID_PRIORITIES = {TODO_PRIORITY_HIGH, TODO_PRIORITY_MEDIUM, TODO_PRIORITY_LOW}


def get_todos_dir() -> Path:
    d = get_workspace() / ".mycode" / "todos"
    d.mkdir(parents=True, exist_ok=True)
    return d


@dataclass
class TodoItem:
    id: int
    content: str
    status: str = TODO_STATUS_PENDING
    priority: str = TODO_PRIORITY_MEDIUM
    created_at: str = ""
    updated_at: str = ""
    
    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
    
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TodoItem:
        return cls(
            id=data.get("id", 0),
            content=data.get("content", ""),
            status=data.get("status", TODO_STATUS_PENDING),
            priority=data.get("priority", TODO_PRIORITY_MEDIUM),
            created_at=data.get("created_at", ""),
            updated_at=data.get("updated_at", ""),
        )


@dataclass
class TodoList:
    session_id: str
    todos: list[TodoItem] = field(default_factory=list)
    next_id: int = 1
    
    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "todos": [t.to_dict() for t in self.todos],
            "next_id": self.next_id,
        }
    
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TodoList:
        return cls(
            session_id=data.get("session_id", ""),
            todos=[TodoItem.from_dict(t) for t in data.get("todos", [])],
            next_id=data.get("next_id", 1),
        )


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_todos(session_id: str) -> TodoList:
    todos_dir = get_todos_dir()
    path = todos_dir / f"{session_id}.json"
    if not path.exists():
        return TodoList(session_id=session_id)
    try:
        data = json.loads(path.read_text())
        return TodoList.from_dict(data)
    except Exception:
        return TodoList(session_id=session_id)


def save_todos(todo_list: TodoList) -> None:
    todos_dir = get_todos_dir()
    path = todos_dir / f"{todo_list.session_id}.json"
    path.write_text(json.dumps(todo_list.to_dict(), indent=2, ensure_ascii=False))


def add_todo(session_id: str, content: str, priority: str = TODO_PRIORITY_MEDIUM) -> TodoItem:
    todo_list = load_todos(session_id)
    now = _now_iso()
    item = TodoItem(
        id=todo_list.next_id,
        content=content,
        status=TODO_STATUS_PENDING,
        priority=priority if priority in VALID_PRIORITIES else TODO_PRIORITY_MEDIUM,
        created_at=now,
        updated_at=now,
    )
    todo_list.todos.append(item)
    todo_list.next_id += 1
    save_todos(todo_list)
    return item


def update_todo(session_id: str, todo_id: int, status: str | None = None, content: str | None = None) -> TodoItem | None:
    todo_list = load_todos(session_id)
    for item in todo_list.todos:
        if item.id == todo_id:
            if status is not None and status in VALID_STATUSES:
                item.status = status
            if content is not None:
                item.content = content
            item.updated_at = _now_iso()
            save_todos(todo_list)
            return item
    return None


def remove_todo(session_id: str, todo_id: int) -> bool:
    todo_list = load_todos(session_id)
    original_len = len(todo_list.todos)
    todo_list.todos = [t for t in todo_list.todos if t.id != todo_id]
    if len(todo_list.todos) < original_len:
        save_todos(todo_list)
        return True
    return False


def list_todos(session_id: str) -> list[TodoItem]:
    todo_list = load_todos(session_id)
    return todo_list.todos
