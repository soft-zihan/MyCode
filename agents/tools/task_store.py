"""TaskList 存储 — JSON 文件持久化。

存储在 ~/.mycode/todos/{session_id}.json（目录名刻意保留，见 get_tasks_dir）。

本模块是纯 JSON IO 层：**禁止 import agents.core.session**。事件 seq 一律由调用方
作为参数传入（`detail_origin_seq` / `current_seq`），以保持 store 与事件流解耦。
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Sequence

from agents.core.workspace import get_workspace


TASK_STATUS_PENDING = "pending"
TASK_STATUS_IN_PROGRESS = "in_progress"
TASK_STATUS_COMPLETED = "completed"
TASK_STATUS_SKIPPED = "skipped"
TASK_STATUS_FAILED = "failed"

VALID_STATUSES = {
    TASK_STATUS_PENDING,
    TASK_STATUS_IN_PROGRESS,
    TASK_STATUS_COMPLETED,
    TASK_STATUS_SKIPPED,
    TASK_STATUS_FAILED,
}

# 旧 JSON 里的 cancelled 归一化为 skipped，不做文件迁移
_LEGACY_STATUS_ALIASES = {"cancelled": TASK_STATUS_SKIPPED}

TASK_PRIORITY_HIGH = "high"
TASK_PRIORITY_MEDIUM = "medium"
TASK_PRIORITY_LOW = "low"

VALID_PRIORITIES = {TASK_PRIORITY_HIGH, TASK_PRIORITY_MEDIUM, TASK_PRIORITY_LOW}


def _normalize_status(value: str) -> str:
    value = _LEGACY_STATUS_ALIASES.get(value, value)
    return value if value in VALID_STATUSES else TASK_STATUS_PENDING


def get_tasks_dir() -> Path:
    # 目录名刻意保留 "todos"：非模型可见契约，改名需迁移回退路径而收益为零
    d = get_workspace() / ".mycode" / "todos"
    d.mkdir(parents=True, exist_ok=True)
    return d


@dataclass
class TaskItem:
    id: int
    content: str
    status: str = TASK_STATUS_PENDING
    priority: str = TASK_PRIORITY_MEDIUM
    created_at: str = ""
    updated_at: str = ""
    detail: str = ""
    acceptance: str = ""
    detail_origin_seq: int | None = None
    started_seq: int | None = None
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TaskItem:
        seq = data.get("detail_origin_seq")
        started = data.get("started_seq")
        return cls(
            id=data.get("id", 0),
            content=data.get("content", ""),
            status=_normalize_status(data.get("status", TASK_STATUS_PENDING)),
            priority=data.get("priority", TASK_PRIORITY_MEDIUM),
            created_at=data.get("created_at", ""),
            updated_at=data.get("updated_at", ""),
            detail=data.get("detail", ""),
            acceptance=data.get("acceptance", ""),
            detail_origin_seq=seq if isinstance(seq, int) else None,
            started_seq=started if isinstance(started, int) else None,
            error=data.get("error", ""),
        )


@dataclass
class TaskList:
    session_id: str
    tasks: list[TaskItem] = field(default_factory=list)
    next_id: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "tasks": [t.to_dict() for t in self.tasks],
            "next_id": self.next_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TaskList:
        raw = data.get("tasks")
        if raw is None:
            raw = data.get("todos", [])  # 旧文件用 "todos" 键
        return cls(
            session_id=data.get("session_id", ""),
            tasks=[TaskItem.from_dict(t) for t in raw],
            next_id=data.get("next_id", 1),
        )


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_tasks(session_id: str) -> TaskList:
    tasks_dir = get_tasks_dir()
    path = tasks_dir / f"{session_id}.json"
    if not path.exists():
        return TaskList(session_id=session_id)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return TaskList.from_dict(data)
    except (json.JSONDecodeError, UnicodeDecodeError, OSError):
        # 读失败不能退化成「空清单」：所有 mutating 调用方都是 load → mutate → save，
        # 空清单会让下一次 save 直接覆盖掉整个文件，静默丢数据。把坏文件挪到一边
        # 保留证据再返回空清单，工具仍可用。异常范围刻意收窄——意料之外的错误
        # 应当抛出来，不该被当成「文件坏了」。
        quarantine = path.with_name(f"{path.stem}.corrupt-{int(time.time())}.json")
        try:
            path.rename(quarantine)
        except OSError:
            pass
        return TaskList(session_id=session_id)


def save_tasks(task_list: TaskList) -> None:
    tasks_dir = get_tasks_dir()
    path = tasks_dir / f"{task_list.session_id}.json"
    path.write_text(
        json.dumps(task_list.to_dict(), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def _insert_after(tasks: list[TaskItem], item: TaskItem, after_id: int | None) -> None:
    """原地插入。after_id=None 追加末尾；0 插到最前；id 不存在则退回追加末尾。"""
    if after_id is None:
        tasks.append(item)
        return
    if after_id == 0:
        tasks.insert(0, item)
        return
    for index, existing in enumerate(tasks):
        if existing.id == after_id:
            tasks.insert(index + 1, item)
            return
    tasks.append(item)


def add_task(
    session_id: str,
    content: str,
    priority: str = TASK_PRIORITY_MEDIUM,
    detail: str = "",
    acceptance: str = "",
    after_id: int | None = None,
) -> TaskItem:
    task_list = load_tasks(session_id)
    now = _now_iso()
    item = TaskItem(
        id=task_list.next_id,
        content=content,
        status=TASK_STATUS_PENDING,
        priority=priority if priority in VALID_PRIORITIES else TASK_PRIORITY_MEDIUM,
        created_at=now,
        updated_at=now,
        detail=detail,
        acceptance=acceptance,
    )
    task_list.next_id += 1
    _insert_after(task_list.tasks, item, after_id)
    save_tasks(task_list)
    return item


def update_task(
    session_id: str,
    task_id: int,
    status: str | None = None,
    content: str | None = None,
    detail: str | None = None,
    acceptance: str | None = None,
    error: str | None = None,
    after_id: int | None = None,
    current_seq: int | None = None,
) -> TaskItem | None:
    # current_seq（写 started_seq）在 Task 8 实现；本任务只接住这个参数，不做任何处理。
    task_list = load_tasks(session_id)
    for item in task_list.tasks:
        if item.id == task_id:
            if status is not None and status in VALID_STATUSES:
                item.status = status
            if content is not None:
                item.content = content
            if detail is not None:
                item.detail = detail
            if acceptance is not None:
                item.acceptance = acceptance
            if error is not None:
                item.error = error
            if after_id is not None and after_id != task_id:
                task_list.tasks = [t for t in task_list.tasks if t.id != task_id]
                _insert_after(task_list.tasks, item, after_id)
            item.updated_at = _now_iso()
            save_tasks(task_list)
            return item
    return None


def remove_task(session_id: str, task_id: int) -> bool:
    task_list = load_tasks(session_id)
    original_len = len(task_list.tasks)
    task_list.tasks = [t for t in task_list.tasks if t.id != task_id]
    if len(task_list.tasks) < original_len:
        save_tasks(task_list)
        return True
    return False


def list_tasks(session_id: str) -> list[TaskItem]:
    task_list = load_tasks(session_id)
    return task_list.tasks


_FOCUS_STATUS_ORDER = (
    TASK_STATUS_IN_PROGRESS,
    TASK_STATUS_FAILED,
    TASK_STATUS_PENDING,
)


def find_focus(tasks: list[TaskItem]) -> TaskItem | None:
    """推导焦点条：in_progress > failed > pending，各档取列表顺序第一个。

    读列表状态而非事件记账，所以乱序完成、中途插条、跳过、resume 都自愈。
    in_progress 优先意味着中途插入的 pending 条不会抢走正在执行任务的焦点。
    failed 排在 pending 之前，是旧 plan_continue 里 has_failed_tasks 硬闸门的
    软版本：不阻塞，但失败条会被披露出来，模型没法假装没看见。
    """
    for status in _FOCUS_STATUS_ORDER:
        for task in tasks:
            if task.status == status:
                return task
    return None


# 注入体积上限。理由：persist_large_result（agents/tools/executor.py:27）把超 30KB
# 的工具结果换成 200 行预览，_truncate_result（agents/tools/registry.py:25-33）在
# 50K 字符处保头尾截断。不限长的 detail 可能把整个结果推过阈值或被切掉尾巴。
# store 里始终存全量，只有注入块被截断。
DETAIL_DISCLOSURE_CHAR_LIMIT = 6000


def clip_detail(detail: str) -> str:
    if len(detail) <= DETAIL_DISCLOSURE_CHAR_LIMIT:
        return detail
    kept = detail[:DETAIL_DISCLOSURE_CHAR_LIMIT]
    dropped = len(detail) - DETAIL_DISCLOSURE_CHAR_LIMIT
    return f"{kept}\n\n[... 已截断 {dropped} 字符；完整方案在 task_list store 里，用 task_list get 取 ...]"


def needs_disclosure(item: TaskItem | None, visible_seqs: Sequence[int]) -> bool:
    """焦点条的 detail 是否需要（重新）注入上下文。

    默认不披露：模型自己写的 detail 已经在它那次 tool_calls 的参数里，折叠前
    一直可见，再注入一份是纯重复。只有承载它的事件已被折叠隐藏、上下文被 clear、
    或从未注入过（detail_origin_seq is None，例如 plan 物化出来的）时才注入。
    """
    if item is None or not item.detail.strip():
        return False
    if item.detail_origin_seq is None:
        return True
    return item.detail_origin_seq not in visible_seqs


def mark_detail_disclosed(session_id: str, task_id: int, seq: int) -> None:
    """记录 detail 已进入上下文的承载事件 seq，作为下次判定的依据。"""
    task_list = load_tasks(session_id)
    for item in task_list.tasks:
        if item.id == task_id:
            item.detail_origin_seq = seq
            save_tasks(task_list)
            return


def format_disclosure_block(item: TaskItem) -> str:
    """把焦点条的详细执行方案拼成注入块。detail 为空时返回空串（不注入）。

    用 <system-reminder> 包裹，与 wiki 召回注入同一约定
    （agents/core/turn_runner.py:117-123）。
    """
    if not item.detail.strip():
        return ""

    lines = [
        "<system-reminder>",
        f"## 当前任务的执行方案（#{item.id} {item.content}）",
    ]
    if item.acceptance.strip():
        lines.append(f"验收: {item.acceptance.strip()}")
    if item.status == TASK_STATUS_FAILED and item.error.strip():
        lines.append(f"上次失败原因: {item.error.strip()}")
    lines.append("")
    lines.append(clip_detail(item.detail))
    lines.append("")
    lines.append(
        f"（自动披露：这段方案已不在你的可见上下文中，故重新注入。"
        f"开始执行时把 #{item.id} 标为 in_progress。）"
    )
    lines.append("</system-reminder>")
    return "\n".join(lines)
