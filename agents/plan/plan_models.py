"""Plan 数据模型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Literal


class PlanStatus(str, Enum):
    PROPOSED = "proposed"
    IN_PROGRESS = "in-progress"
    COMPLETED = "completed"
    ARCHIVED = "archived"
    ABANDONED = "abandoned"


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
    status: Literal["pending", "done", "failed"]
    error: str = ""
    session_id: str = ""
    retry_count: int = 0


@dataclass
class PlanRecallResult:
    plan: Plan
    score: float
    match_reason: str = ""
