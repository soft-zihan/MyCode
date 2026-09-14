"""轻量 trace 事件日志 — 用于监控运行状态与定位问题。

设计：
- OTel 导出：通过 OpenTelemetry 导出到 Langfuse（OTLP/HTTP）
- JSONL 兜底：trace_event() 写入 ~/.mycode/trace/，与 Span 双轨（Span 看链路，JSONL 看离散事件）
- session_id 来源：MYCODE_TRACE_SESSION 环境变量 / set_trace_session() 设置
- 开启方式：启动参数 --trace / 环境变量 MYCODE_TRACE=1 / REPL 内 /trace on
- 关闭时零开销：trace_event 立即返回，不做任何 IO

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
import time
from datetime import datetime
from pathlib import Path
from typing import Any


# ============================================================
# 全局状态
# ============================================================

_enabled: bool | None = None
_session_id: str | None = None
_session_created_at: str | None = None


# ============================================================
# Session / 路径
# ============================================================


def trace_dir() -> Path:
    override = os.environ.get("MYCODE_TRACE_DIR", "").strip()
    if override:
        return Path(override)
    return Path.home() / ".mycode" / "trace"


def set_trace_session(session_id: str) -> None:
    global _session_id, _session_created_at
    _session_id = session_id
    _session_created_at = datetime.now().strftime("%Y%m%d_%H%M%S")
    os.environ["MYCODE_TRACE_SESSION"] = session_id


def get_trace_session() -> str | None:
    global _session_id
    if _session_id is None:
        _session_id = os.environ.get("MYCODE_TRACE_SESSION", "").strip() or None
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
    """记录 trace 事件（JSONL 兜底日志，与 OTel Span 双轨）。

    OTel/Langfuse 看执行链路与耗时，JSONL 看离散事件细节（审计、调试）。
    写入 ~/.mycode/trace/{ts}_{session}.jsonl，失败静默（观测不影响主流程）。
    """
    if not trace_enabled():
        return
    try:
        record = {
            "ts": datetime.now().astimezone().isoformat(timespec="milliseconds"),
            "kind": kind,
            **fields,
        }
        path = trace_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except Exception:
        pass


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
    return os.environ.get("MYCODE_OTEL", "").strip() not in ("", "0")


class TraceSpan:
    """Trace span 上下文管理器 — 只写入 OTel。
    
    业务代码只调用这个 API，不需要知道 OTel 的存在。
    kind 自动映射为 Langfuse observation type（generation/tool/agent/guardrail/chain）。
    """

    # kind → Langfuse observation type（langfuse.observation.type）
    _KIND_TYPE_MAP = {
        "model_call": "generation",
        "llm": "generation",
        "tool_call": "tool",
        "tool": "tool",
        "turn": "chain",
        "compact": "chain",
        "context.compaction": "chain",
        "sub_agent": "agent",
        "audit": "guardrail",
    }

    def __init__(self, kind: str, **attributes: Any):
        self._kind = kind
        self._attributes = attributes
        self._otel_cm = None  # OTel 上下文管理器
        self._otel_span = None
        self._start_time: float | None = None

    def _langfuse_attrs(self) -> dict[str, Any]:
        """将业务 kind 映射为 Langfuse 规范属性。"""
        attrs = dict(self._attributes)
        prefix = self._kind.split(".")[0]
        obs_type = self._KIND_TYPE_MAP.get(self._kind) or self._KIND_TYPE_MAP.get(prefix)
        if obs_type:
            attrs.setdefault("langfuse.observation.type", obs_type)
        if obs_type == "generation":
            model = attrs.get("model")
            if model:
                attrs.setdefault("langfuse.observation.model.name", str(model))
        return attrs

    def __enter__(self) -> "TraceSpan":
        self._start_time = time.time()
        # 创建 OTel span（如果启用）
        if otel_enabled():
            try:
                from agents.observability.tracer import tracer
                self._otel_cm = tracer.span(self._kind, self._langfuse_attrs())
                self._otel_span = self._otel_cm.__enter__()
            except Exception:
                pass  # OTel 初始化失败不影响主流程
        return self
    
    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        duration_s = time.time() - self._start_time if self._start_time else 0
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
# 查询 API（从 JSONL 兜底日志读取；Span 级查询走 Langfuse API/UI）
# ============================================================


def recent_events(n: int = 20) -> list[dict]:
    """读取最近的 trace 事件（JSONL 兜底日志）。"""
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
    """格式化最近的 trace 事件。"""
    events = recent_events(n)
    otel_state = "ON→Langfuse" if otel_enabled() else "OFF"
    header = f"Trace: JSONL({'ON' if trace_enabled() else 'OFF'}) OTel({otel_state})"
    if not events:
        return header + "\n(no events yet)"
    lines = []
    for e in events:
        ts = e.get("ts", "")[11:23]
        kind = e.get("kind", "?")
        rest = {k: v for k, v in e.items() if k not in ("ts", "kind")}
        lines.append(f"{ts}  {kind:<12} {json.dumps(rest, ensure_ascii=False, default=str)}")
    return header + "\n" + "\n".join(lines)
