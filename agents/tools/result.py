from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

ToolStatus = Literal["ok", "error", "cancelled", "denied"]
ToolOutcome = Literal["success", "error", "timeout", "cancelled", "blocked", "budget_exceeded"]


@dataclass(slots=True)
class ToolExecutionResult:
    text: str
    status: ToolStatus = "ok"
    outcome: ToolOutcome = "success"
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def failed(self) -> bool:
        return self.status != "ok" or self.outcome != "success"
