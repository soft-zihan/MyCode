"""U3b 自动唤醒预算（v2 restart.ts:26-33 per-turn 预算思想）。

语义：无真实 user_message 间隔的连续 trigger=auto_wake turn/start 不得超过
AUTO_WAKE_BUDGET——防"完成通知→唤醒→再派发→再通知"无限自启循环。
从事件日志派生（单一数据源）：崩溃恢复后天然一致，无需内存计数落盘。
预算耗尽后通知仍可靠落账（subagent/completed 事件），下一个用户轮次模型
自动可见——只是不再自动起轮。
"""

from __future__ import annotations

from typing import Any, Iterable

AUTO_WAKE_BUDGET = 3


def auto_wake_budget_exhausted(events: Iterable[dict[str, Any]]) -> bool:
    """自最近一条真实 user_message 以来的 auto_wake turn 数是否已达预算。"""
    count = 0
    for event in reversed(list(events)):
        etype = event.get("type")
        if etype == "user_message":
            break
        if etype == "turn/start" and event.get("trigger") == "auto_wake":
            count += 1
    return count >= AUTO_WAKE_BUDGET
