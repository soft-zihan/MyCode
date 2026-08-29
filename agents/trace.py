"""轻量 trace 事件日志 — 用于监控运行状态与定位问题。

设计：
- JSONL 写入：~/.bear-code/trace/{session_id}.jsonl
- session_id 来源：BEAR_TRACE_SESSION 环境变量 / set_trace_session() 设置
- 如果未设置 session_id，使用日期作为文件名（向后兼容）
- 开启方式：启动参数 --trace / 环境变量 BEAR_TRACE=1 / REPL 内 /trace on
- 线程安全（后台 shell watcher 线程也会发事件）
- 关闭时零开销：trace_event 立即返回，不做任何 IO

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
from typing import Any

_LOCK = threading.Lock()
_enabled: bool | None = None  # 懒初始化：首次使用时读环境变量
_session_id: str | None = None  # 当前 session ID


def trace_dir() -> Path:
    override = os.environ.get("BEAR_TRACE_DIR", "").strip()
    if override:
        return Path(override)
    return Path.home() / ".bear-code" / "trace"


def set_trace_session(session_id: str) -> None:
    """设置当前 trace 的 session ID，用于生成独立文件名。"""
    global _session_id
    _session_id = session_id
    os.environ["BEAR_TRACE_SESSION"] = session_id


def get_trace_session() -> str | None:
    """获取当前 session ID。"""
    global _session_id
    if _session_id is None:
        _session_id = os.environ.get("BEAR_TRACE_SESSION", "").strip() or None
    return _session_id


def trace_path() -> Path:
    session = get_trace_session()
    if session:
        return trace_dir() / f"{session}.jsonl"
    return trace_dir() / f"{datetime.now():%Y-%m-%d}.jsonl"


def trace_enabled() -> bool:
    global _enabled
    if _enabled is None:
        _enabled = os.environ.get("BEAR_TRACE", "").strip() not in ("", "0")
    return _enabled


def set_trace_enabled(value: bool) -> None:
    global _enabled
    _enabled = bool(value)


def trace_event(kind: str, **fields: Any) -> None:
    """追加一条事件。写入失败静默跳过——trace 绝不能影响主流程。"""
    if not trace_enabled():
        return
    event: dict[str, Any] = {"ts": datetime.now().isoformat(timespec="milliseconds"), "kind": kind}
    event.update(fields)
    try:
        with _LOCK:
            trace_dir().mkdir(parents=True, exist_ok=True)
            with open(trace_path(), "a", encoding="utf-8") as f:
                f.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
    except OSError:
        pass


def _preview(value: Any, limit: int = 300) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= limit else text[:limit] + "…"


def trace_tool_input(inp: dict) -> str:
    """工具输入的 trace 摘要（截断，避免把大文件内容写进日志）。"""
    return _preview(inp)


def recent_events(n: int = 20) -> list[dict]:
    """读取今天 trace 文件的最后 n 条事件。"""
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
    """把最近 n 条事件格式化成可读文本（/trace 命令用）。"""
    events = recent_events(n)
    header = f"Trace file: {trace_path()} ({'ON' if trace_enabled() else 'OFF'})"
    if not events:
        return header + "\n(no events yet)"
    lines = []
    for e in events:
        ts = e.get("ts", "")[11:23]  # HH:MM:SS.mmm
        kind = e.get("kind", "?")
        rest = {k: v for k, v in e.items() if k not in ("ts", "kind")}
        lines.append(f"{ts}  {kind:<12} {json.dumps(rest, ensure_ascii=False, default=str)}")
    return header + "\n" + "\n".join(lines)
