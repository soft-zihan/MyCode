"""Scheduler for cron-based tasks and heartbeat.

Features:
- Cron 表达式解析
- 定时任务触发
- 周期性心跳
- 任务持久化
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Awaitable

from agents.observability.trace import trace_event


DEFAULT_SCHEDULE_FILE = Path.home() / ".bear-code" / "schedules.json"


@dataclass
class ScheduledTask:
    """定时任务定义。"""
    task_id: str
    cron: str
    command: str
    session_id: str | None = None
    enabled: bool = True
    last_run: float = 0.0
    next_run: float = 0.0
    run_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "cron": self.cron,
            "command": self.command,
            "session_id": self.session_id,
            "enabled": self.enabled,
            "last_run": self.last_run,
            "next_run": self.next_run,
            "run_count": self.run_count,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ScheduledTask:
        return cls(
            task_id=data["task_id"],
            cron=data["cron"],
            command=data["command"],
            session_id=data.get("session_id"),
            enabled=data.get("enabled", True),
            last_run=data.get("last_run", 0.0),
            next_run=data.get("next_run", 0.0),
            run_count=data.get("run_count", 0),
        )


class CronParser:
    """简单的 cron 表达式解析器。

    支持格式: minute hour day month weekday
    例如:
    - "*/5 * * * *"  每 5 分钟
    - "0 * * * *"    每小时整点
    - "0 0 * * *"    每天午夜
    - "0 9 * * 1-5"  工作日 9 点
    """

    FIELD_RANGES = {
        "minute": (0, 59),
        "hour": (0, 23),
        "day": (1, 31),
        "month": (1, 12),
        "weekday": (0, 6),  # 0 = Sunday
    }

    FIELD_NAMES = ["minute", "hour", "day", "month", "weekday"]

    def __init__(self, expression: str):
        self.expression = expression
        self.fields = self._parse(expression)

    def _parse(self, expression: str) -> dict[str, set[int]]:
        """解析 cron 表达式为字段集合。"""
        parts = expression.strip().split()
        if len(parts) != 5:
            raise ValueError(f"Invalid cron expression: {expression}")

        fields = {}
        for i, (name, part) in enumerate(zip(self.FIELD_NAMES, parts)):
            min_val, max_val = self.FIELD_RANGES[name]
            fields[name] = self._parse_field(part, min_val, max_val)

        return fields

    def _parse_field(self, field: str, min_val: int, max_val: int) -> set[int]:
        """解析单个字段。"""
        values: set[int] = set()

        # 处理逗号分隔
        for part in field.split(","):
            # 处理步长 */n
            if "/" in part:
                base, step = part.split("/", 1)
                step = int(step)
                if base == "*":
                    start = min_val
                else:
                    start = int(base)
                values.update(range(start, max_val + 1, step))

            # 处理范围 n-m
            elif "-" in part:
                start, end = part.split("-", 1)
                values.update(range(int(start), int(end) + 1))

            # 处理通配符 *
            elif part == "*":
                values.update(range(min_val, max_val + 1))

            # 处理单个值
            else:
                values.add(int(part))

        return values

    def matches(self, dt: datetime | None = None) -> bool:
        """检查给定时间是否匹配 cron 表达式。"""
        if dt is None:
            dt = datetime.now()

        return (
            dt.minute in self.fields["minute"]
            and dt.hour in self.fields["hour"]
            and dt.day in self.fields["day"]
            and dt.month in self.fields["month"]
            and dt.weekday() in self.fields["weekday"]  # Python weekday: 0=Monday
        )

    def next_occurrence(self, after: datetime | None = None) -> datetime:
        """计算下一次触发的时间。"""
        if after is None:
            after = datetime.now()

        # 简单实现：逐分钟检查（最多检查 1 年）
        current = after.replace(second=0, microsecond=0)
        for _ in range(525600):  # 1 年的分钟数
            current = current.replace(minute=current.minute + 1)
            if current.minute >= 60:
                current = current.replace(minute=0, hour=current.hour + 1)
            if current.hour >= 24:
                current = current.replace(hour=0, day=current.day + 1)
            # 简化：不处理月份边界

            if self.matches(current):
                return current

        # 如果找不到，返回当前时间 + 1 天
        return after.replace(hour=0, minute=0, second=0, microsecond=0)


class Scheduler:
    """任务调度器。"""

    def __init__(self, schedule_file: Path = DEFAULT_SCHEDULE_FILE):
        self.schedule_file = schedule_file
        self.tasks: dict[str, ScheduledTask] = {}
        self._running = False
        self._task: asyncio.Task | None = None
        self._on_trigger: Callable[[ScheduledTask], Awaitable[None]] | None = None

    def load(self) -> None:
        """从文件加载任务列表。"""
        if not self.schedule_file.exists():
            return

        try:
            data = json.loads(self.schedule_file.read_text())
            for item in data.get("tasks", []):
                task = ScheduledTask.from_dict(item)
                self.tasks[task.task_id] = task
        except Exception:
            pass

    def save(self) -> None:
        """保存任务列表到文件。"""
        self.schedule_file.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "tasks": [t.to_dict() for t in self.tasks.values()],
        }
        self.schedule_file.write_text(json.dumps(data, indent=2))

    def add_task(
        self,
        task_id: str,
        cron: str,
        command: str,
        session_id: str | None = None,
    ) -> ScheduledTask:
        """添加定时任务。"""
        # 验证 cron 表达式
        parser = CronParser(cron)
        next_run = parser.next_occurrence()

        task = ScheduledTask(
            task_id=task_id,
            cron=cron,
            command=command,
            session_id=session_id,
            next_run=next_run.timestamp(),
        )
        self.tasks[task_id] = task
        self.save()

        trace_event("scheduler.add_task", task_id=task_id, cron=cron)
        return task

    def remove_task(self, task_id: str) -> bool:
        """移除定时任务。"""
        if task_id in self.tasks:
            del self.tasks[task_id]
            self.save()
            trace_event("scheduler.remove_task", task_id=task_id)
            return True
        return False

    def list_tasks(self) -> list[ScheduledTask]:
        """列出所有任务。"""
        return list(self.tasks.values())

    def set_trigger(self, callback: Callable[[ScheduledTask], Awaitable[None]]) -> None:
        """设置任务触发回调。"""
        self._on_trigger = callback

    async def start(self) -> None:
        """启动调度器主循环。"""
        self._running = True
        self.load()

        while self._running:
            now = time.time()

            for task in list(self.tasks.values()):
                if not task.enabled:
                    continue

                if task.next_run <= now:
                    # 触发任务
                    if self._on_trigger:
                        try:
                            await self._on_trigger(task)
                        except Exception as e:
                            trace_event("scheduler.trigger_error", task_id=task.task_id, error=str(e))

                    # 更新下次运行时间
                    parser = CronParser(task.cron)
                    task.next_run = parser.next_occurrence().timestamp()
                    task.last_run = now
                    task.run_count += 1
                    self.save()

            await asyncio.sleep(30)  # 每 30 秒检查一次

    async def stop(self) -> None:
        """停止调度器。"""
        self._running = False
        if self._task:
            self._task.cancel()


class Heartbeat:
    """周期性心跳。"""

    def __init__(self, interval_seconds: int = 60):
        self.interval = interval_seconds
        self._running = False
        self._task: asyncio.Task | None = None
        self._on_beat: Callable[[], Awaitable[None]] | None = None
        self._beat_count = 0

    def set_callback(self, callback: Callable[[], Awaitable[None]]) -> None:
        """设置心跳回调。"""
        self._on_beat = callback

    async def start(self) -> None:
        """启动心跳循环。"""
        self._running = True
        while self._running:
            await asyncio.sleep(self.interval)
            self._beat_count += 1

            if self._on_beat:
                try:
                    await self._on_beat()
                except Exception as e:
                    trace_event("heartbeat.error", error=str(e))

            trace_event("heartbeat.beat", count=self._beat_count)

    async def stop(self) -> None:
        """停止心跳。"""
        self._running = False

    @property
    def beat_count(self) -> int:
        return self._beat_count
