"""取消的「执行结果」与「传输结果」分开记。

abort 只设标志并 cancel 当前 task，模型调用、工具、子进程还要一会儿才真的停下来。
REST 端点过去是「设标志 → 立刻写 turn/cancel → 返回」，于是事件流里这个 turn 已经
被记成干净闭合，而工具可能还在跑、之后还会往闭合之后追加事件——把传输结果当成了
执行结果。OpenAgentCore 对这条讲得最死：拿不到 native settlement 就记
cancel_unconfirmed，「never becomes applied=true」；Vercel 的 relay 是先杀掉 session
的 cgroup、**然后才**把仍开着的调用记为 interrupted。

这里只认事件流：晚于取消时刻出现 turn 收尾事件，才算「真的停了」。
（WS 那条 abort 路径不写任何收尾事件，收尾由 turn_runner 在 CancelledError 分支里
写 turn/end{aborted}，本来就是诚实的；要说谎的只有这个 REST 端点。）
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Protocol

from .event_types import TURN_CLOSER_TYPES

# 10s 与 OpenAgentCore 的 settlement 超时同量级：够一次模型请求被 cancel 后
# 走完 finally（含子进程清理），又不至于让调用方以为端点挂了。
CANCEL_SETTLE_TIMEOUT_S = 10.0
CANCEL_SETTLE_POLL_S = 0.05


class _EventStream(Protocol):
    def replay(self, since_seq: int = 0) -> Any: ...


def settled_since(session: _EventStream, since_seq: int) -> bool:
    """since_seq 之后是否已经出现 turn 收尾事件。"""
    return any(
        event.get("type") in TURN_CLOSER_TYPES for event in session.replay(since_seq)
    )


async def wait_for_turn_settlement(
    session: _EventStream,
    since_seq: int,
    timeout_s: float | None = None,
) -> bool:
    """等事件流承认「这个 turn 真的停了」。超时返回 False，不猜。"""
    limit = CANCEL_SETTLE_TIMEOUT_S if timeout_s is None else timeout_s
    deadline = time.monotonic() + limit
    while not settled_since(session, since_seq):
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(CANCEL_SETTLE_POLL_S)
    return True
