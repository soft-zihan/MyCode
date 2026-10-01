"""Plan 数据模型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Literal


class PlanStatus(str, Enum):
    PROPOSED = "proposed"
    IN_PROGRESS = "in-progress"
    PAUSED = "paused"
    COMPLETED = "completed"
    READY_TO_ARCHIVE = "ready_to_archive"
    ARCHIVED = "archived"
    ABANDONED = "abandoned"
    CONVERGE_EXHAUSTED = "converge_exhausted"


class PlanGranularity(str, Enum):
    MINIMAL = "minimal"
    STANDARD = "standard"
    FULL = "full"


@dataclass
class Plan:
    __slots__ = ("slug", "status", "priority", "created", "modified", "tags",
                 "granularity", "plan_dir")

    slug: str
    status: PlanStatus
    priority: str
    created: str
    modified: str
    tags: list[str]
    granularity: PlanGranularity
    plan_dir: str

    @property
    def is_active(self) -> bool:
        return self.status in (PlanStatus.PROPOSED, PlanStatus.IN_PROGRESS)


@dataclass
class Task:
    id: int
    description: str
    status: Literal["pending", "in-progress", "done", "failed", "skipped"]
    error: str = ""
    session_id: str = ""
    retry_count: int = 0
    file: str = ""
    function: str = ""
    interface: str = ""
    acceptance: str = ""
    duration_s: int = 0
    # 任务块里未被已知标记（**文件**/**函数**/**接口**/**验收**/**状态**/**错误**/
    # **重试次数**）识别的其余行，由 _parse_structured_tasks 收集（Plan 3a Task 3）。
    # 轻量轨的 `- 改动:`/`- 注意:` 缩进子项经 checkbox_tasks_to_structured 转换后
    # 就落在这里，物化时与结构化字段一起组合进 TaskItem.detail。
    # _parse_simple_tasks（checkbox 格式）没有块结构可收集，body 保持空。
    body: str = ""


@dataclass
class PlanRecallResult:
    plan: Plan
    score: float
    match_reason: str = ""
