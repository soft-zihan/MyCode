"""Spinner — OpenCode-aligned braille dot animation."""

from __future__ import annotations

import threading
import time

from .output import _safe_stdout_write
from .theme import theme

SPINNER_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

_spinner_thread: threading.Thread | None = None
_spinner_stop = threading.Event()
_spinner_label = "Thinking"


def start_spinner(label: str = "Thinking") -> None:
    global _spinner_thread, _spinner_label
    if _spinner_thread is not None:
        return
    _spinner_label = label
    _spinner_stop.clear()

    def _run() -> None:
        frame = 0
        _safe_stdout_write(f"\r  {SPINNER_FRAMES[0]} {_spinner_label}...")
        while not _spinner_stop.is_set():
            time.sleep(0.08)
            frame = (frame + 1) % len(SPINNER_FRAMES)
            _safe_stdout_write(f"\r  {SPINNER_FRAMES[frame]} {_spinner_label}...")

    _spinner_thread = threading.Thread(target=_run, daemon=True)
    _spinner_thread.start()


def stop_spinner() -> None:
    global _spinner_thread
    if _spinner_thread is None:
        return
    _spinner_stop.set()
    _spinner_thread.join(timeout=1)
    _spinner_thread = None
    _safe_stdout_write("\r\033[K")
