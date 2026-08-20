"""trace 事件日志 + thinking 滑动窗口物理行擦除。"""

from __future__ import annotations

import json

import pytest

from agents import trace, ui


# ─── trace 模块 ─────────────────────────────────────────────

def test_trace_disabled_by_default_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("BEAR_TRACE", "")
    monkeypatch.setenv("BEAR_TRACE_DIR", str(tmp_path))
    trace.set_trace_enabled(False)
    trace.trace_event("test.event", foo=1)
    assert not list(tmp_path.glob("*.jsonl"))


def test_trace_enabled_writes_jsonl(tmp_path, monkeypatch):
    monkeypatch.setenv("BEAR_TRACE_DIR", str(tmp_path))
    trace.set_trace_enabled(True)
    try:
        trace.trace_event("turn.start", turn=1, user_preview="hi")
        trace.trace_event("tool.end", tool="read_file", duration_s=0.1)
        files = list(tmp_path.glob("*.jsonl"))
        assert len(files) == 1
        lines = files[0].read_text(encoding="utf-8").splitlines()
        assert len(lines) == 2
        e1 = json.loads(lines[0])
        assert e1["kind"] == "turn.start"
        assert e1["turn"] == 1
        assert "ts" in e1
    finally:
        trace.set_trace_enabled(False)


def test_recent_events_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("BEAR_TRACE_DIR", str(tmp_path))
    trace.set_trace_enabled(True)
    try:
        for i in range(5):
            trace.trace_event("evt", n=i)
        events = trace.recent_events(3)
        assert len(events) == 3
        assert [e["n"] for e in events] == [2, 3, 4]
    finally:
        trace.set_trace_enabled(False)


def test_format_recent_events_contains_kind(tmp_path, monkeypatch):
    monkeypatch.setenv("BEAR_TRACE_DIR", str(tmp_path))
    trace.set_trace_enabled(True)
    try:
        trace.trace_event("bg.done", job_id="job1", exit_code=0)
        text = trace.format_recent_events(10)
        assert "bg.done" in text
        assert "job1" in text
    finally:
        trace.set_trace_enabled(False)


def test_trace_tool_input_truncates():
    big = {"content": "x" * 1000}
    assert len(trace.trace_tool_input(big)) <= 301


def test_format_recent_events_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("BEAR_TRACE_DIR", str(tmp_path / "nodir"))
    trace.set_trace_enabled(False)
    text = trace.format_recent_events(5)
    assert "no events yet" in text


# ─── thinking 滑动窗口：物理行擦除 ──────────────────────────

def _reset_thinking():
    ui.reset_thinking_window()


def test_thinking_window_single_prefix(monkeypatch):
    """UI 统一加 [thinking] 前缀，内容里不应再嵌套前缀。"""
    _reset_thinking()
    written = []
    monkeypatch.setattr(ui, "_safe_stdout_write", written.append)
    ui.print_thinking_text("hello world")
    # 首次绘制有分隔换行
    assert "\n" in written
    # 缓冲区不含前缀标记
    assert "[thinking]" not in ui._thinking_buffer


def test_thinking_window_phys_rows_for_long_line(monkeypatch):
    """长行自动折行时，擦除行数按物理行计算（>逻辑行数）。"""
    _reset_thinking()
    ui.console = type("C", (), {"width": 40, "print": staticmethod(lambda *a, **k: None)})()
    written = []
    monkeypatch.setattr(ui, "_safe_stdout_write", written.append)
    # 一条 200 字符的单行 thinking：40 宽终端折成 5+ 物理行
    ui.print_thinking_text("x" * 200)
    assert ui._thinking_drawn_rows >= 5
    # 第二次增量：应先擦除物理行数
    written.clear()
    ui.print_thinking_text("y")
    assert any("\033[" in w and "A" in w for w in written)


def test_thinking_window_keeps_last_n_lines():
    _reset_thinking()
    ui.print_thinking_text("\n".join(f"line{i}" for i in range(20)))
    lines = ui._thinking_buffer.split("\n")[-ui._THINKING_WINDOW_LINES:]
    assert lines[-1] == "line19"
    assert len(lines) == ui._THINKING_WINDOW_LINES


def test_thinking_reset_clears_state():
    ui.print_thinking_text("some thinking")
    ui.reset_thinking_window()
    assert ui._thinking_buffer == ""
    assert ui._thinking_drawn_rows == 0
    assert ui._thinking_started is False
