"""评测事件流：子进程按行落盘，后端 tail 之后转发到 WebSocket。

为什么是文件而不是直接发 WS：评测子进程是 `start_new_session=True` 起的独立进程，
活得比 web 服务久（服务重启不能杀掉跑了几小时的任务）。文件是唯一能跨这个边界的通道，
而且顺带成了"这轮到底发生了什么"的可回放记录。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Callable, Protocol, runtime_checkable

EVENTS_PATH_ENV = "MYCODE_EVAL_EVENTS_PATH"
RUN_ID_ENV = "MYCODE_EVAL_RUN_ID"


@runtime_checkable
class EventSink(Protocol):
    def emit(self, type: str, **payload: Any) -> None: ...


class NullSink:
    """直接敲 CLI 时用这个：不落文件、不改变任何现有行为。"""

    def emit(self, type: str, **payload: Any) -> None:
        return None


class JsonlEventSink:
    def __init__(self, path: Path | str, run_id: str) -> None:
        self.path = Path(path)
        self.run_id = run_id
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # 行缓冲不够，必须每行 flush：后端 tailer 要实时看到进度，
        # 而重定向到文件时 Python 默认是全缓冲的（harbor runner 已为此踩过坑）
        self._fh = self.path.open("a", encoding="utf-8")

    def emit(self, type: str, **payload: Any) -> None:
        # 信封字段放在**后面**：调用方万一传了 run_id= / ts= 这样的同名 kwarg，
        # 被覆盖掉的就是 run 归属和时间戳，前端靠它们过滤和排序——静默丢弃比报错难查得多
        record = {**payload, "type": type, "run_id": self.run_id, "ts": time.time()}
        self._fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        self._fh.flush()

    def close(self) -> None:
        if not self._fh.closed:
            self._fh.close()


def sink_from_env(get: Callable[[str], str | None] = os.environ.get) -> EventSink:
    path = get(EVENTS_PATH_ENV)
    if not path:
        return NullSink()
    return JsonlEventSink(path, get(RUN_ID_ENV) or "")


def read_events(path: Path | str, since: int = 0) -> tuple[list[dict[str, Any]], int]:
    """从字节 offset `since` 开始读事件，返回 (事件列表, 新 offset)。

    末尾可能是被 kill 时写了一半的行：只吃到最后一个换行符为止，
    offset 也只推进到那里，半行留给下次补齐。
    """
    p = Path(path)
    if not p.exists():
        return [], 0
    size = p.stat().st_size
    if since >= size:
        return [], since
    with p.open("r", encoding="utf-8", errors="replace") as fh:
        fh.seek(since)
        chunk = fh.read(size - since)
    consumed = chunk.rfind("\n")
    if consumed < 0:
        return [], since
    events: list[dict[str, Any]] = []
    for line in chunk[: consumed + 1].splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events, since + len(chunk[: consumed + 1].encode("utf-8"))
