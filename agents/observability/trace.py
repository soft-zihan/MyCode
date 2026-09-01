"""轻量 trace 事件日志 — 用于监控运行状态与定位问题。

设计：
- JSONL 写入：~/.bear-code/trace/{session_id}.jsonl
- session_id 来源：BEAR_TRACE_SESSION 环境变量 / set_trace_session() 设置
- 如果未设置 session_id，使用日期作为文件名（向后兼容）
- 开启方式：启动参数 --trace / 环境变量 BEAR_TRACE=1 / REPL 内 /trace on
- 线程安全（后台 shell watcher 线程也会发事件）
- 关闭时零开销：trace_event 立即返回，不做任何 IO
- 支持可替换 Sink（JsonlFileSink / NoopSink / 自定义）

事件 schema：{"ts": ISO8601, "kind": str, ...fields}
常用 kind：
  turn.start / turn.end   对话回合（含 token 增量、耗时、是否中止）
  tool.start / tool.end   工具调用（含输入摘要、结果预览、耗时）
  bg.start / bg.done      后台 shell 任务
  compact                 上下文压缩
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol


# ============================================================
# Sink 抽象
# ============================================================


class TraceSink(Protocol):
    def emit(self, event: dict[str, Any]) -> None: ...


class JsonlFileSink:
    """默认 Sink：写入 JSONL 文件。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()

    def emit(self, event: dict[str, Any]) -> None:
        try:
            with self._lock:
                trace_dir().mkdir(parents=True, exist_ok=True)
                with open(trace_path(), "a", encoding="utf-8") as f:
                    f.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
        except OSError:
            pass


class NoopSink:
    """空 Sink：丢弃所有事件。"""

    def emit(self, event: dict[str, Any]) -> None:
        pass


# ============================================================
# 全局状态
# ============================================================

_LOCK = threading.Lock()
_enabled: bool | None = None
_session_id: str | None = None
_sink: TraceSink = JsonlFileSink()


def set_trace_sink(sink: TraceSink) -> None:
    """替换 trace 事件的后端 Sink。"""
    global _sink
    _sink = sink


def get_trace_sink() -> TraceSink:
    return _sink


# ============================================================
# Session / 路径
# ============================================================


def trace_dir() -> Path:
    override = os.environ.get("BEAR_TRACE_DIR", "").strip()
    if override:
        return Path(override)
    return Path.home() / ".bear-code" / "trace"


def set_trace_session(session_id: str) -> None:
    global _session_id
    _session_id = session_id
    os.environ["BEAR_TRACE_SESSION"] = session_id


def get_trace_session() -> str | None:
    global _session_id
    if _session_id is None:
        _session_id = os.environ.get("BEAR_TRACE_SESSION", "").strip() or None
    return _session_id


def trace_path() -> Path:
    session = get_trace_session()
    if session:
        return trace_dir() / f"{session}.jsonl"
    return trace_dir() / f"{datetime.now():%Y-%m-%d}.jsonl"


# ============================================================
# 启用/禁用
# ============================================================


def trace_enabled() -> bool:
    global _enabled
    if _enabled is None:
        _enabled = os.environ.get("BEAR_TRACE", "").strip() not in ("", "0")
    return _enabled


def set_trace_enabled(value: bool) -> None:
    global _enabled
    _enabled = bool(value)


# ============================================================
# 核心 API
# ============================================================


def trace_event(kind: str, **fields: Any) -> None:
    if not trace_enabled():
        return
    event: dict[str, Any] = {
        "ts": datetime.now().isoformat(timespec="milliseconds"),
        "kind": kind,
    }
    event.update(fields)
    _sink.emit(event)


def _preview(value: Any, limit: int = 300) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= limit else text[:limit] + "…"


def trace_tool_input(inp: dict) -> str:
    return _preview(inp)


# ============================================================
# 查询 API
# ============================================================


def recent_events(n: int = 20) -> list[dict]:
    path = trace_path()
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    events = []
    for line in lines[-n:]:
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


def format_recent_events(n: int = 20) -> str:
    events = recent_events(n)
    header = f"Trace file: {trace_path()} ({'ON' if trace_enabled() else 'OFF'})"
    if not events:
        return header + "\n(no events yet)"
    lines = []
    for e in events:
        ts = e.get("ts", "")[11:23]
        kind = e.get("kind", "?")
        rest = {k: v for k, v in e.items() if k not in ("ts", "kind")}
        lines.append(f"{ts}  {kind:<12} {json.dumps(rest, ensure_ascii=False, default=str)}")
    return header + "\n" + "\n".join(lines)
