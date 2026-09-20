"""Wiki commit 批处理 — 多次写入合并为一个 commit。

参考：projects/llm-wiki-memory/scripts/lib/wiki-commit.mjs
"""

from __future__ import annotations

import subprocess
from contextlib import contextmanager
from pathlib import Path
from typing import Generator

from agents.wiki.wiki_manager import get_wiki_dir


_BATCH_STACK: list[list[str]] = []


@contextmanager
def wiki_commit_batch(message: str) -> Generator[None, None, None]:
    """批处理 commit 上下文。

    在上下文内的所有写入操作会被合并为一个 commit。
    """
    wiki_dir = get_wiki_dir()
    _BATCH_STACK.append([])

    try:
        yield
    finally:
        changes = _BATCH_STACK.pop()
        if changes:
            _do_commit(wiki_dir, message, changes)


def record_wiki_change(rel_path: str) -> None:
    """记录一次 wiki 变更。"""
    if _BATCH_STACK:
        _BATCH_STACK[-1].append(rel_path)


def _do_commit(wiki_dir: Path, message: str, changes: list[str]) -> None:
    """执行 git commit。"""
    try:
        subprocess.run(
            ["git", "add"] + changes,
            cwd=wiki_dir, capture_output=True, timeout=10,
        )
        subprocess.run(
            ["git", "commit", "-m", message, "--allow-empty"],
            cwd=wiki_dir, capture_output=True, timeout=10,
        )
    except Exception:
        pass
