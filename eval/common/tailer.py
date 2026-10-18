"""把评测子进程的事件文件转发上 WebSocket。

子进程是 `start_new_session=True` 起的，活得比后端久；文件是它们之间唯一的通道。
这里只负责「读新增的行 → 广播」，不解析语义：事件长什么样由写它的那一方决定，
tailer 认识的就只有 `read_events` 给的 offset。
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Callable

from eval.common.events import read_events
from eval.common.jobs import events_path, reap_finished, running_jobs

logger = logging.getLogger(__name__)

POLL_INTERVAL_S = 0.5


class JobTailer:
    def __init__(
        self,
        broadcast: Callable[[dict[str, Any]], None],
        *,
        poll_interval_s: float = POLL_INTERVAL_S,
        reports_dir: Path | None = None,
    ) -> None:
        self._broadcast = broadcast
        self._poll_interval_s = poll_interval_s
        self._reports_dir = reports_dir
        self._offsets: dict[str, int] = {}

    def tracked(self) -> list[str]:
        return sorted(self._offsets)

    def poll_once(self) -> int:
        """走一轮：收尸 → 跟踪表对齐活跃 job → 广播新增事件，返回条数。"""
        # 先收尸再算 active：跑完的子进程没人 wait 就一直是 running，
        # 闸门永久关着、列表页永远显示"在跑"。收完这一轮仍会读完它的剩余事件，
        # 摘掉跟踪要等下一轮"不活跃且无新事件"，最后几条不会丢
        reaped = reap_finished(self._reports_dir)
        if reaped:
            logger.info("eval tailer reaped finished jobs: %s", ", ".join(reaped))
        active = {rec.run_id for rec in running_jobs(self._reports_dir)}
        for run_id in active - self._offsets.keys():
            self._offsets[run_id] = 0

        emitted = 0
        for run_id in self.tracked():
            events, offset = read_events(
                events_path(run_id, self._reports_dir), self._offsets[run_id]
            )
            self._offsets[run_id] = offset
            for event in events:
                # 一个订阅者断了不能带走这批剩下的事件：页面会永久停在半路，
                # 而且看不出是哪儿停的
                try:
                    self._broadcast({"session_id": None, **event})
                except Exception as e:
                    logger.warning("eval tailer broadcast failed run=%s: %s", run_id, e)
                emitted += 1
            if run_id not in active and not events:
                # 已经排干且不再活跃，摘掉；下一次同名 run 会从 0 重新跟
                del self._offsets[run_id]
        return emitted

    async def run(self) -> None:
        while True:
            self.poll_once()
            await asyncio.sleep(self._poll_interval_s)
