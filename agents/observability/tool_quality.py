"""工具调用质量追踪 — 实时规则检测 + Phoenix 离线分析

实时检测：重复调用、危险操作、参数错误
离线分析：通过 Phoenix Dataset + Experiment 评估工具选择正确性
"""

from __future__ import annotations


class ToolQualityTracker:

    def __init__(self, window: int = 20):
        self.recent_calls: list[dict] = []
        self._window = window

    def check_before_call(self, tool: str, inp: dict) -> dict:
        issues = []

        if self._is_duplicate(tool, inp):
            issues.append("duplicate_call")

        dangerous = self._check_dangerous(tool, inp)
        if dangerous:
            issues.append(f"dangerous:{dangerous}")

        return {
            "issues": issues,
            "should_warn": len(issues) > 0,
        }

    def record_call(self, tool: str, inp: dict, success: bool, error: str = ""):
        self.recent_calls.append({
            "tool": tool,
            "input": inp,
            "success": success,
            "error": error,
        })
        if len(self.recent_calls) > self._window:
            self.recent_calls = self.recent_calls[-self._window:]

    def get_stats(self) -> dict:
        if not self.recent_calls:
            return {"total": 0, "success_rate": 0.0, "failure_rate": 0.0}

        total = len(self.recent_calls)
        successes = sum(1 for c in self.recent_calls if c["success"])
        return {
            "total": total,
            "successes": successes,
            "failures": total - successes,
            "success_rate": round(successes / total, 4),
            "failure_rate": round((total - successes) / total, 4),
        }

    def _is_duplicate(self, tool: str, inp: dict) -> bool:
        for call in self.recent_calls[-3:]:
            if call["tool"] == tool and call["input"] == inp:
                return True
        return False

    def _check_dangerous(self, tool: str, inp: dict) -> str:
        if tool == "run_shell":
            cmd = inp.get("command", "")
            patterns = {
                "rm -rf": "rm_rf",
                "mkfs": "mkfs",
                "dd if=": "dd_overwrite",
                ":(){": "fork_bomb",
                "chmod -R 777": "chmod_777",
                "> /dev/": "dev_write",
            }
            for pattern, label in patterns.items():
                if pattern in cmd:
                    return label
        return ""


_tracker: ToolQualityTracker | None = None


def get_tool_quality_tracker() -> ToolQualityTracker:
    global _tracker
    if _tracker is None:
        _tracker = ToolQualityTracker()
    return _tracker
