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
  model.start / model.end 模型调用（含 token、耗时、成功/失败）
  bg.start / bg.done      后台 shell 任务
  compact                 上下文压缩
  error                   异常/错误（含 error_type, message, traceback）
  timeout                 超时事件（含 operation, timeout_s）
  system                  系统事件（容器状态、进程状态等）
"""

from __future__ import annotations

import json
import os
import threading
import time
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
_session_created_at: str | None = None
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
    global _session_id, _session_created_at
    _session_id = session_id
    _session_created_at = datetime.now().strftime("%Y%m%d_%H%M%S")
    os.environ["BEAR_TRACE_SESSION"] = session_id


def get_trace_session() -> str | None:
    global _session_id
    if _session_id is None:
        _session_id = os.environ.get("BEAR_TRACE_SESSION", "").strip() or None
    return _session_id


def trace_path() -> Path:
    session = get_trace_session()
    if session:
        ts = _session_created_at or datetime.now().strftime("%Y%m%d_%H%M%S")
        return trace_dir() / f"{ts}_{session}.jsonl"
    return trace_dir() / f"{datetime.now():%Y-%m-%d}.jsonl"


# ============================================================
# 启用/禁用
# ============================================================


def trace_enabled() -> bool:
    global _enabled
    if _enabled is None:
        _enabled = True
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
# 便捷 API：错误、超时、系统事件
# ============================================================


def trace_error(
    error_type: str,
    message: str,
    operation: str = "",
    traceback_str: str = "",
    **extra: Any,
) -> None:
    """记录异常/错误事件。
    
    Args:
        error_type: 错误类型（timeout, connection_error, exception, api_error 等）
        message: 错误消息
        operation: 发生错误的操作（model_call, tool_call 等）
        traceback_str: 堆栈跟踪（可选）
        **extra: 额外字段
    """
    trace_event(
        "error",
        error_type=error_type,
        message=message,
        operation=operation,
        traceback=traceback_str[:2000] if traceback_str else "",
        **extra,
    )


def trace_timeout(
    operation: str,
    timeout_s: float,
    elapsed_s: float = 0,
    **extra: Any,
) -> None:
    """记录超时事件。
    
    Args:
        operation: 超时操作（model_call, tool_call, shell_command 等）
        timeout_s: 超时阈值（秒）
        elapsed_s: 实际耗时（秒）
        **extra: 额外字段
    """
    trace_event(
        "timeout",
        operation=operation,
        timeout_s=timeout_s,
        elapsed_s=elapsed_s,
        **extra,
    )


# ============================================================
# OTel 桥接：Span 上下文管理器
# ============================================================


def otel_enabled() -> bool:
    """检查 OTel 是否启用。"""
    return os.environ.get("BEAR_OTEL", "").strip() not in ("", "0")


class TraceSpan:
    """Trace span 上下文管理器 — 同时写入 JSONL 和 OTel。
    
    业务代码只调用这个 API，不需要知道 OTel 的存在。
    """
    
    def __init__(self, kind: str, **attributes: Any):
        self._kind = kind
        self._attributes = attributes
        self._otel_cm = None  # OTel 上下文管理器
        self._otel_span = None
        self._start_time: float | None = None
    
    def __enter__(self) -> "TraceSpan":
        self._start_time = time.time()
        # 写入 JSONL start 事件
        trace_event(f"{self._kind}.start", **self._attributes)
        # 创建 OTel span（如果启用）
        if otel_enabled():
            try:
                from agents.observability.otel_exporter import otel_span
                self._otel_cm = otel_span(self._kind, self._attributes)
                self._otel_span = self._otel_cm.__enter__()
            except Exception:
                pass  # OTel 初始化失败不影响主流程
        return self
    
    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        duration_s = time.time() - self._start_time if self._start_time else 0
        # 写入 JSONL end 事件（包含所有属性）
        end_attrs = dict(self._attributes)
        end_attrs["duration_s"] = round(duration_s, 2)
        end_attrs["success"] = exc_type is None
        if exc_type:
            end_attrs["error"] = str(exc_val)[:300]
        trace_event(f"{self._kind}.end", **end_attrs)
        # 结束 OTel span（如果启用）
        if self._otel_cm:
            try:
                if exc_type and self._otel_span:
                    self._otel_span.set_status("ERROR", str(exc_val)[:300])
                self._otel_cm.__exit__(exc_type, exc_val, exc_tb)
            except Exception:
                pass  # OTel 错误不影响主流程
        return None  # 不吞异常
    
    def set_attribute(self, key: str, value: Any) -> None:
        """设置 span 属性。"""
        self._attributes[key] = value
        if self._otel_span:
            try:
                self._otel_span.set_attribute(key, value)
            except Exception:
                pass
    
    def record_error(self, error: Exception) -> None:
        """记录错误到 span。"""
        if self._otel_span:
            try:
                self._otel_span.set_status("ERROR", str(error)[:300])
                self._otel_span.record_exception(error)
            except Exception:
                pass


def trace_span(kind: str, **attributes: Any) -> TraceSpan:
    """创建 trace span 上下文管理器。
    
    用法：
        with trace_span("model_call", model="gpt-4") as span:
            # 执行业务逻辑
            span.set_attribute("tokens", 100)
    """
    return TraceSpan(kind, **attributes)



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
