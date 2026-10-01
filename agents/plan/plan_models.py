"""Plan 数据模型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Literal


class PlanStatus(str, Enum):
    """计划的生命周期状态。

    **这是「盘上已有 plan 目录」的反序列化面，不是「当前代码写过哪些状态」的清单。**
    裁剪任何一个成员都必须配一次迁移（把盘上 `_meta.md` 里的旧值改写掉），否则
    后果是静默的：_parse_plan_dir 的 `PlanStatus(meta.get("status", "proposed"))` 抛
    ValueError → 被它那个 blanket `except Exception: return None`（plan_manager.py:90-91）
    吞掉 → get_plan 返回 None → 那份计划在面板上凭空不见，且一条错误都不报。
    真正的脆弱点是那个 blanket except（它把「读不回的旧数据」与「目录不存在」压成同一个
    None），不是这个枚举——已记入 later plan 跟踪，此处刻意不动。

    Task 4 删掉了执行状态机，裁剪清单本要把这里收缩成五个成员。实际只删了
    CONVERGE_EXHAUSTED，判据是「这个值有没有可能已经躺在用户盘上的 `_meta.md` 里」：

    - CONVERGE_EXHAUSTED 从没被任何代码写过（Converge 阶段根本没实现过），盘上不可能
      有它，删掉是纯收益。
    - PAUSED 仍被**保留**的 pause_plan / resume_plan 读写（frontend/server/routers/
      sessions.py 的两个 REST 端点在调），删了它们当场 AttributeError。
    - READY_TO_ARCHIVE 曾由已发布的 plan_complete 工具经 update_plan_status 写盘。
      _parse_plan_dir 用 `PlanStatus(meta["status"])` 反序列化，成员一旦消失就抛
      ValueError → 被 except 吞成 None → get_plan 返回 None，那份计划在面板上凭空
      不见。COMPLETED / ARCHIVED 在 Task 4 之后同样没有任何写入方，裁剪清单却保留了
      它们——可见这个枚举本来就承担「读得回旧数据」的职责，READY_TO_ARCHIVE 属于同一类。

    等 Plan 3b 把 pause/resume/abandon 那 8 个端点一起删掉时，可以连 PAUSED 重新评估。
    """
    PROPOSED = "proposed"
    IN_PROGRESS = "in-progress"
    PAUSED = "paused"
    COMPLETED = "completed"
    READY_TO_ARCHIVE = "ready_to_archive"
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
