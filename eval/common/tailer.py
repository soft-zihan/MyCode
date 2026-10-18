"""把评测子进程的事件文件转发上 WebSocket。

子进程是 `start_new_session=True` 起的，活得比后端久；文件是它们之间唯一的通道。
这里只负责「读新增的行 → 广播」，不解析语义：事件长什么样由写它的那一方决定，
tailer 认识的就只有 `read_events` 给的 offset。
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from typing import Any, Callable

from eval.common.events import read_events
from eval.common.jobs import events_path, list_jobs, reap_finished, running_jobs

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
        # 索引一轮只扫一次，收尸和算活跃共用这份快照。list_jobs 要遍历整个 reports
        # 目录、逐个解析 job.json（本机 7 条记录实测 23.4/24.0/28.1 ms），扫两遍就是
        # 每 0.5s 往事件循环上压 ~47-56ms——而同一个循环还在流式吐 token、发 WS 帧。
        # 代价是这一轮的 active 是收尸**之前**的快照：被本轮收掉的那条要多跟一轮才摘，
        # 而"多跟一轮"正是排干最后几条事件所依赖的方向，不会丢事件
        records = list_jobs(limit=1000, reports_dir=self._reports_dir)
        # 先收尸再算 active：跑完的子进程没人 wait 就一直是 running，
        # 闸门永久关着、列表页永远显示"在跑"。收完这一轮仍会读完它的剩余事件，
        # 摘掉跟踪要等下一轮"不活跃且无新事件"，最后几条不会丢
        reaped = reap_finished(self._reports_dir, records=records)
        if reaped:
            # print 而不是 logger.info：全仓没有任何 logging 配置，root logger 停在
            # WARNING 且没有 handler，INFO 会被 lastResort 直接丢掉——而这行是
            # "收尸者还活着"的唯一证据。每轮最多打一次，不是噪音
            print(f"[EVAL] tailer 收尸: {', '.join(reaped)}", flush=True)
        active = {rec.run_id for rec in running_jobs(self._reports_dir, records=records)}
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
            try:
                self.poll_once()
            except Exception:
                # 一轮失败不能带走 tailer：它是系统里唯一的周期收尸者，死了之后
                # 页面冻结在 half-way，MAX_CONCURRENT_JOBS=1 的闸门也再不会重开。
                # 而 main.py 持着这个 task 的唯一强引用，asyncio 那条
                # "Task exception was never retrieved" 要到关机才可能打出来——
                # 也就是说冻结期间运维本来会拿到零诊断
                logger.exception("eval tailer poll failed")
                print("[EVAL] tailer poll 失败（继续下一轮）", file=sys.stderr, flush=True)
            # sleep 必须留在 try 外面：放进去会让一轮进行中的 poll 拖慢取消，
            # 而 lifespan 关机时正是对这个 task 调 .cancel()。
            # CancelledError 挂在 BaseException 下（3.8+），上面的 except Exception 抓不到它
            await asyncio.sleep(self._poll_interval_s)
