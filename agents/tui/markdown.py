from __future__ import annotations

import os
import re
import sys

from rich.cells import cell_len
from rich.markdown import Markdown

from .output import console, _safe_text, _safe_stdout_write

_MD_FEATURES = re.compile(
    r"```|^#{1,6}\s|\n\s*[-*+]\s|\n\s*\d+\.\s|\*\*|^\|.+\|\s*$|\[.+\]\(.+\)",
    re.MULTILINE,
)

_md_text = ""
_md_rows = 0
_md_col = 0


def md_render_enabled() -> bool:
    """BEAR_MD_RENDER=0 可关闭；非 TTY（管道/重定向）也关闭。"""
    if os.environ.get("BEAR_MD_RENDER", "").strip() == "0":
        return False
    try:
        return sys.stdout.isatty()
    except Exception:
        return False


def md_looks_renderable(text: str) -> bool:
    """文本足够长且含 Markdown 特征时才值得重渲染。"""
    return len(text.strip()) >= 40 and bool(_MD_FEATURES.search(text))


def _md_count_rows(text: str, col: int, width: int) -> tuple[int, int]:
    """计算把 text 写到 (当前行, col) 之后新增的行数与结束列。

    考虑终端自动换行（按显示宽度 cell_len，CJK 全角字符算 2 列）。
    """
    rows = 0
    for i, seg in enumerate(text.split("\n")):
        if i > 0:
            rows += 1
            col = 0
        if seg:
            cells = cell_len(seg)
            total = col + cells
            if total > 0:
                rows += (total - 1) // width
            col = total % width
    return rows, col


def md_track_begin() -> None:
    """每轮模型流式响应开始时重置跟踪状态。"""
    global _md_text, _md_rows, _md_col
    _md_text, _md_rows, _md_col = "", 0, 0


def md_track_feed(text: str) -> None:
    """流式片段与打印同步喂入，累计文本和占用行数。"""
    global _md_text, _md_rows, _md_col
    _md_text += text
    width = max(console.width, 20)
    added, _md_col = _md_count_rows(text, _md_col, width)
    _md_rows += added


def md_track_flush() -> None:
    """本轮最终回复结束：擦除原始流式文本，重渲染为 Markdown。

    不满足条件（非 TTY / 无 Markdown 特征 / 被禁用）时保持原文不动。
    """
    global _md_text, _md_rows, _md_col
    text, rows = _md_text, _md_rows
    _md_text, _md_rows, _md_col = "", 0, 0
    if not text or not md_render_enabled() or not md_looks_renderable(text):
        return
    if rows > 0:
        _safe_stdout_write(f"\033[{rows}A")
    _safe_stdout_write("\r\033[J")
    console.print(Markdown(_safe_text(text)))
