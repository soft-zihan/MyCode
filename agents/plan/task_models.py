"""Plan Task 模型 — 结构化任务定义。

每个 Task 包含精确的文件、函数、接口、验收标准。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass
class StructuredTask:
    """结构化任务定义。"""
    id: int
    title: str
    file: str
    function: str
    interface: str
    acceptance: str
    status: str = "pending"
    duration_s: int = 0
    created_at: str = ""
    updated_at: str = ""
    
    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "file": self.file,
            "function": self.function,
            "interface": self.interface,
            "acceptance": self.acceptance,
            "status": self.status,
            "duration_s": self.duration_s,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
    
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> StructuredTask:
        return cls(
            id=data.get("id", 0),
            title=data.get("title", ""),
            file=data.get("file", ""),
            function=data.get("function", ""),
            interface=data.get("interface", ""),
            acceptance=data.get("acceptance", ""),
            status=data.get("status", "pending"),
            duration_s=data.get("duration_s", 0),
            created_at=data.get("created_at", ""),
            updated_at=data.get("updated_at", ""),
        )
    
    def to_markdown(self) -> str:
        """转换为 markdown 格式。"""
        status_icon = {"pending": "[ ]", "in-progress": "[~]", "done": "[x]", "failed": "[!]"}.get(self.status, "[ ]")
        return f"""### Task {self.id}: {self.title}
- **文件**: `{self.file}`
- **函数**: `{self.function}`
- **接口**: `{self.interface}`
- **验收**: `{self.acceptance}`
- **状态**: {status_icon} {self.status}
- **耗时**: {self.duration_s}s
"""


@dataclass
class LedgerEntry:
    """执行 Ledger 条目。"""
    task_id: int
    status: str
    started: str
    finished: str = ""
    duration_s: int = 0
    subagent_session: str = ""
    commit: str = ""
    review_rounds: int = 0
    verification: dict[str, Any] = field(default_factory=dict)
    failure_reason: str = ""
    
    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "status": self.status,
            "started": self.started,
            "finished": self.finished,
            "duration_s": self.duration_s,
            "subagent_session": self.subagent_session,
            "commit": self.commit,
            "review_rounds": self.review_rounds,
            "verification": self.verification,
            "failure_reason": self.failure_reason,
        }
    
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> LedgerEntry:
        return cls(
            task_id=data.get("task_id", 0),
            status=data.get("status", ""),
            started=data.get("started", ""),
            finished=data.get("finished", ""),
            duration_s=data.get("duration_s", 0),
            subagent_session=data.get("subagent_session", ""),
            commit=data.get("commit", ""),
            review_rounds=data.get("review_rounds", 0),
            verification=data.get("verification", {}),
            failure_reason=data.get("failure_reason", ""),
        )


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_tasks_from_markdown(content: str) -> list[StructuredTask]:
    """从 markdown 内容解析结构化任务。"""
    import re
    
    tasks = []
    task_pattern = r"### Task (\d+): ([^\n]+)"
    
    for match in re.finditer(task_pattern, content):
        task_id = int(match.group(1))
        title = match.group(2).strip()
        
        start = match.end()
        next_match = re.search(r"### Task \d+:", content[start:])
        end = start + next_match.start() if next_match else len(content)
        task_block = content[match.start():end]
        
        file_match = re.search(r"\*\*文件\*\*:\s*`?([^`\n]+)`?", task_block)
        function_match = re.search(r"\*\*函数\*\*:\s*`?([^`\n]+)`?", task_block)
        interface_match = re.search(r"\*\*接口\*\*:\s*`?([^`\n]+)`?", task_block)
        acceptance_match = re.search(r"\*\*验收\*\*:\s*`?([^`\n]+)`?", task_block)
        status_match = re.search(r"\*\*状态\*\*:\s*\[[ x~!]\]\s*(\w+)", task_block)
        
        task = StructuredTask(
            id=task_id,
            title=title,
            file=file_match.group(1).strip() if file_match else "",
            function=function_match.group(1).strip() if function_match else "",
            interface=interface_match.group(1).strip() if interface_match else "",
            acceptance=acceptance_match.group(1).strip() if acceptance_match else "",
            status=status_match.group(1).strip() if status_match else "pending",
        )
        tasks.append(task)
    
    return tasks
