"""Subagent progress tracking for interactive viewing.

Tracks subagent tasks and their status, allowing users to view progress.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable


class SubagentStatus(Enum):
    """Subagent execution status."""
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    ABORTED = "aborted"


@dataclass
class SubagentTask:
    """A subagent task with progress tracking."""
    agent_id: str
    agent_type: str
    description: str
    status: SubagentStatus = SubagentStatus.RUNNING
    start_time: float = field(default_factory=time.time)
    end_time: float | None = None
    duration_ms: float | None = None
    tokens: int = 0
    result: str | None = None
    error: str | None = None
    # Progress tracking
    tool_calls: int = 0
    last_tool: str = ""
    
    @property
    def elapsed_ms(self) -> float:
        """Get elapsed time in milliseconds."""
        if self.end_time:
            return (self.end_time - self.start_time) * 1000
        return (time.time() - self.start_time) * 1000
    
    @property
    def is_running(self) -> bool:
        return self.status == SubagentStatus.RUNNING
    
    def complete(self, result: str, tokens: int = 0) -> None:
        """Mark task as completed."""
        self.status = SubagentStatus.COMPLETED
        self.end_time = time.time()
        self.duration_ms = self.elapsed_ms
        self.result = result
        self.tokens = tokens
    
    def fail(self, error: str) -> None:
        """Mark task as failed."""
        self.status = SubagentStatus.FAILED
        self.end_time = time.time()
        self.duration_ms = self.elapsed_ms
        self.error = error
    
    def abort(self) -> None:
        """Mark task as aborted."""
        self.status = SubagentStatus.ABORTED
        self.end_time = time.time()
        self.duration_ms = self.elapsed_ms
    
    def update_progress(self, tool_name: str) -> None:
        """Update progress with latest tool call."""
        self.tool_calls += 1
        self.last_tool = tool_name


class SubagentTracker:
    """Tracks all subagent tasks for the current session."""
    
    def __init__(self):
        self._tasks: dict[str, SubagentTask] = {}
        self._on_change: Callable[[], None] | None = None
    
    def set_change_callback(self, callback: Callable[[], None]) -> None:
        """Set callback to be called when tasks change."""
        self._on_change = callback
    
    def _notify_change(self) -> None:
        """Notify listener of changes."""
        if self._on_change:
            self._on_change()
    
    def start_task(self, agent_id: str, agent_type: str, description: str) -> SubagentTask:
        """Start tracking a new subagent task."""
        task = SubagentTask(
            agent_id=agent_id,
            agent_type=agent_type,
            description=description,
        )
        self._tasks[agent_id] = task
        self._notify_change()
        return task
    
    def complete_task(self, agent_id: str, result: str, tokens: int = 0) -> None:
        """Mark a task as completed."""
        if task := self._tasks.get(agent_id):
            task.complete(result, tokens)
            self._notify_change()
    
    def fail_task(self, agent_id: str, error: str) -> None:
        """Mark a task as failed."""
        if task := self._tasks.get(agent_id):
            task.fail(error)
            self._notify_change()
    
    def abort_task(self, agent_id: str) -> None:
        """Mark a task as aborted."""
        if task := self._tasks.get(agent_id):
            task.abort()
            self._notify_change()
    
    def update_progress(self, agent_id: str, tool_name: str) -> None:
        """Update task progress."""
        if task := self._tasks.get(agent_id):
            task.update_progress(tool_name)
            self._notify_change()
    
    def get_task(self, agent_id: str) -> SubagentTask | None:
        """Get a task by ID."""
        return self._tasks.get(agent_id)
    
    def get_running_tasks(self) -> list[SubagentTask]:
        """Get all running tasks."""
        return [t for t in self._tasks.values() if t.is_running]
    
    def get_all_tasks(self) -> list[SubagentTask]:
        """Get all tasks, sorted by start time (newest first)."""
        return sorted(self._tasks.values(), key=lambda t: t.start_time, reverse=True)
    
    def get_recent_tasks(self, limit: int = 10) -> list[SubagentTask]:
        """Get recent tasks."""
        return self.get_all_tasks()[:limit]
    
    def clear_completed(self) -> int:
        """Clear completed/failed/aborted tasks. Returns count cleared."""
        before = len(self._tasks)
        self._tasks = {k: v for k, v in self._tasks.items() if v.is_running}
        self._notify_change()
        return before - len(self._tasks)
    
    def has_running_tasks(self) -> bool:
        """Check if there are any running tasks."""
        return any(t.is_running for t in self._tasks.values())


# Global tracker instance
_tracker: SubagentTracker | None = None


def get_tracker() -> SubagentTracker:
    """Get the global subagent tracker."""
    global _tracker
    if _tracker is None:
        _tracker = SubagentTracker()
    return _tracker


def reset_tracker() -> None:
    """Reset the global tracker (for testing)."""
    global _tracker
    _tracker = None
