"""Thinking/reasoning display — OpenCode-aligned collapsible design."""

from __future__ import annotations

import time
from rich.text import Text

from .output import console, _safe_stdout_write
from .theme import theme

_THINKING_WINDOW_LINES = 20
_thinking_buffer = ""
_thinking_drawn_rows = 0
_thinking_started = False
_thinking_summary = ""
_thinking_expanded = False
_thinking_start_time = 0.0


def print_thinking_text(text: str) -> None:
    global _thinking_buffer, _thinking_drawn_rows, _thinking_started, _thinking_summary, _thinking_start_time

    # 记录开始时间
    if _thinking_start_time == 0.0:
        _thinking_start_time = time.time()

    _thinking_buffer += text
    
    # 提取第一行作为摘要
    if not _thinking_summary and _thinking_buffer:
        first_line = _thinking_buffer.split("\n")[0].strip()
        if first_line and len(first_line) < 100:
            _thinking_summary = first_line[:60] + "..." if len(first_line) > 60 else first_line

    if _thinking_expanded:
        display_lines = _thinking_buffer.split("\n")
    else:
        display_lines = _thinking_buffer.split("\n")[-_THINKING_WINDOW_LINES:]

    width = max(console.width, 20)

    from rich.cells import cell_len
    rendered = [f"  {line}" for line in display_lines]
    phys_rows = sum(max(1, -(-cell_len(r) // width)) for r in rendered)

    if _thinking_drawn_rows > 0:
        _safe_stdout_write(f"\033[{_thinking_drawn_rows}A\r\033[J")
    elif not _thinking_started:
        # 显示 header：+ Thought: summary
        header_text = "+ Thought"
        if _thinking_summary:
            header_text += f": {_thinking_summary}"
        header = Text(header_text, style=theme.WARNING)
        console.print(header)
        _thinking_started = True

    for r in rendered:
        console.print(Text(r, style=f"italic {theme.TEXT_MUTED}"))
    _thinking_drawn_rows = phys_rows


def reset_thinking_window() -> None:
    global _thinking_buffer, _thinking_drawn_rows, _thinking_started, _thinking_summary, _thinking_expanded, _thinking_start_time
    _thinking_buffer = ""
    _thinking_drawn_rows = 0
    _thinking_started = False
    _thinking_summary = ""
    _thinking_expanded = False
    _thinking_start_time = 0.0


def toggle_thinking_expanded() -> None:
    global _thinking_expanded
    _thinking_expanded = not _thinking_expanded


def get_thinking_duration() -> str:
    """获取 thinking 持续时间。"""
    if _thinking_start_time == 0.0:
        return ""
    duration = time.time() - _thinking_start_time
    if duration < 1.0:
        return f"{duration*1000:.0f}ms"
    return f"{duration:.1f}s"


_show_thinking = False


def set_thinking_visible(value: bool) -> None:
    global _show_thinking
    _show_thinking = bool(value)


def thinking_visible() -> bool:
    return _show_thinking
