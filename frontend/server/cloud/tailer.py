"""把沙箱 Job 写的会话 JSONL 转发上 WebSocket。

沙箱与控制面之间唯一的通道就是 PVC 上这份文件（同节点、同卷、不同 subPath）。
形状照 eval/common/tailer.py 的 JobTailer：只读新增的行 → 广播，不解析语义。
**不 import eval/**：镜像刻意裁掉 eval/（Dockerfile 的 COPY 清单），import 会在云端炸。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from . import task_store as ts

_SUFFIX = ".events.jsonl"


def read_task_events(task_id: str, limit: int = 200) -> list[dict[str, Any]]:
    """读某任务已落盘的事件（末尾 limit 条）。给 `mycode cloud logs` 用。

    不走 /api/sessions/{id}/events：沙箱的会话文件在 cloud_tasks/<id>/sessions/ 下，
    控制面的 session_manager 扫的是自己的 sessions 根，那边查不到这些会话。
    """
    d = ts.task_dir(task_id) / "sessions"
    if not d.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for p in sorted(d.glob(f"*{_SUFFIX}"), key=lambda x: x.stat().st_mtime):
        events, _ = read_new_lines(p, 0)
        out.extend(events)
    return out[-limit:] if limit > 0 else out


def read_new_lines(path: Path, offset: int) -> tuple[list[dict[str, Any]], int]:
    """从 offset 读新增的**完整**行。半截行不消费——写入方可能还没写完。"""
    if not path.is_file():
        return [], offset
    with path.open("rb") as f:
        f.seek(offset)
        data = f.read()
    end = data.rfind(b"\n")
    if end < 0:
        return [], offset
    events: list[dict[str, Any]] = []
    for line in data[: end + 1].decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            events.append(obj)
    return events, offset + end + 1


class TaskTailer:
    def __init__(self, broadcast: Callable[[dict[str, Any], str | None], None]) -> None:
        self._broadcast = broadcast
        self._offsets: dict[str, int] = {}      # key = f"{task_id}:{文件名}"
        self._sessions: dict[str, str] = {}
        self._tracked: set[str] = set()

    def tracked(self) -> list[str]:
        return sorted(self._tracked)

    def session_id_for(self, task: ts.CloudTask) -> str:
        return self._sessions.get(task.id, "")

    def _session_files(self, task: ts.CloudTask) -> list[Path]:
        d = ts.task_dir(task.id) / "sessions"
        if not d.is_dir():
            return []
        # 主会话 = 最早出现的那个文件；其余（子代理）事件也路由到主会话的订阅者
        return sorted(d.glob(f"*{_SUFFIX}"), key=lambda p: p.stat().st_mtime)

    def _drop(self, task_id: str) -> None:
        self._tracked.discard(task_id)
        self._sessions.pop(task_id, None)
        for k in [k for k in self._offsets if k.startswith(f"{task_id}:")]:
            del self._offsets[k]

    def poll_once(self) -> int:
        active = {t.id: t for t in ts.active_tasks()}
        self._tracked |= active.keys()
        emitted = 0
        for tid in self.tracked():
            task = active.get(tid) or ts.load_task(tid)
            if task is None:
                self._drop(tid)
                continue
            files = self._session_files(task)
            if files and tid not in self._sessions:
                self._sessions[tid] = files[0].name[: -len(_SUFFIX)]
                ts.update_task(tid, None, session_id=self._sessions[tid])
            target = self._sessions.get(tid) or None
            got_any = False
            for p in files:
                key = f"{tid}:{p.name}"
                events, off = read_new_lines(p, self._offsets.get(key, 0))
                self._offsets[key] = off
                for ev in events:
                    got_any = True
                    emitted += 1
                    try:
                        self._broadcast(ev, target)
                    except Exception:
                        # 一个订阅者断了不能带走这批剩下的事件
                        pass
            # 终态任务要先把剩余事件排空再摘掉，否则最后几条永远发不出去
            if tid not in active and not got_any:
                self._drop(tid)
        return emitted