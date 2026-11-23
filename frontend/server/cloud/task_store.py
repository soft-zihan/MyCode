"""云端任务表 — cloud_tasks/<id>/task.json 的读写与状态机。

纯 JSON IO：**不 import session、不 import k8s**。事件流由沙箱 Job 自己写进
同一个目录下的 sessions/，tailer 负责读；本模块只管任务元数据。

为什么不用会话后端当任务表：会话是 agent 的事件流，任务是「一次云端执行」的
编排记录（repo/ref/job/PR），两者生命周期与字段集都不同。混在一起会让
list_sessions() 扫到沙箱会话，造成前端串台。
"""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from dataclasses import asdict, dataclass, fields as dc_fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agents.core import state_root

TASK_STATUSES = (
    "queued", "provisioning", "running", "landing",
    "succeeded", "failed", "timeout", "cancelled",
)
TERMINAL_STATUSES = frozenset({"succeeded", "failed", "timeout", "cancelled"})

# 合法迁移。终态是吸收态：没有出边，所以「跑完的任务又被改回 running」不可能发生。
_TRANSITIONS: dict[str, frozenset[str]] = {
    "queued": frozenset({"provisioning", "failed", "cancelled"}),
    # provisioning 能直跳 landing：轮询间隔 2s，短任务的 Job 可能在被观测到
    # Running 之前就已经 Succeeded。
    "provisioning": frozenset({"running", "landing", "failed", "cancelled"}),
    "running": frozenset({"landing", "failed", "timeout", "cancelled"}),
    # 超时的任务也要落地（半成品不该丢），所以落地出口含 timeout。
    "landing": frozenset({"succeeded", "failed", "timeout"}),
    "succeeded": frozenset(),
    "failed": frozenset(),
    "timeout": frozenset(),
    "cancelled": frozenset(),
}


@dataclass
class CloudTask:
    id: str
    repo_url: str
    prompt: str
    ref: str | None = None
    setup_command: str = ""
    status: str = "queued"
    job_name: str = ""
    session_id: str = ""
    workspace_name: str = ""
    branch: str = ""
    pr_url: str = ""
    mergeable: bool | None = None
    error: str = ""
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CloudTask":
        known = {f.name for f in dc_fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})


def tasks_root() -> Path:
    return state_root.cloud_tasks_dir()


def task_dir(task_id: str) -> Path:
    return tasks_root() / task_id


def new_task_id() -> str:
    return uuid.uuid4().hex[:12]


def _now() -> str:
    # 微秒而非秒：同一秒内连提几个任务时，list_tasks 的倒序要能分出先后。
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def create_task(repo_url: str, prompt: str, ref: str | None = None, setup_command: str = "") -> CloudTask:
    t = CloudTask(
        id=new_task_id(), repo_url=repo_url, prompt=prompt, ref=ref,
        setup_command=setup_command, created_at=_now(), updated_at=_now(),
    )
    save_task(t)
    return t


def save_task(task: CloudTask) -> None:
    d = task_dir(task.id)
    d.mkdir(parents=True, exist_ok=True)
    target = d / "task.json"
    # 原子写：reconciler 与 REST 会并发写同一条记录，半截 JSON 会让任务永久读不出来
    fd, tmp = tempfile.mkstemp(dir=str(d), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(task.to_dict(), f, ensure_ascii=False, indent=2)
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def load_task(task_id: str) -> CloudTask | None:
    p = task_dir(task_id) / "task.json"
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # 隔离而不是抛：一条坏记录不能带走整个列表
        try:
            p.replace(p.with_suffix(".json.bad"))
        except OSError:
            pass
        return None
    if not isinstance(data, dict):
        return None
    return CloudTask.from_dict(data)


def list_tasks(limit: int = 50) -> list[CloudTask]:
    root = tasks_root()
    if not root.is_dir():
        return []
    out: list[CloudTask] = []
    for d in root.iterdir():
        if not d.is_dir():
            continue
        try:
            t = load_task(d.name)
        except Exception:
            # 一条读不出来的记录不能带走整个列表——reconcile 靠这个列表推进所有任务
            continue
        if t is not None:
            out.append(t)
    out.sort(key=lambda x: x.created_at, reverse=True)
    return out[:limit]


def active_tasks() -> list[CloudTask]:
    return [t for t in list_tasks(limit=1000) if t.status not in TERMINAL_STATUSES]


def update_task(task_id: str, status: str | None = None, **fields: Any) -> CloudTask | None:
    t = load_task(task_id)
    if t is None:
        return None
    known = {f.name for f in dc_fields(CloudTask)}
    for k in fields:
        if k not in known or k in ("id", "created_at"):
            raise ValueError(f"不可写字段：{k}")
    if status is not None:
        if status not in TASK_STATUSES:
            raise ValueError(f"未知状态：{status}")
        if status != t.status and status not in _TRANSITIONS[t.status]:
            raise ValueError(f"非法迁移：{t.status} → {status}")
        t.status = status
    for k, v in fields.items():
        setattr(t, k, v)
    t.updated_at = _now()
    save_task(t)
    return t