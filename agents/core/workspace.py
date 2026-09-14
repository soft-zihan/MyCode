"""工作区上下文 — 每会话工作目录的运行时单一来源。

背景：服务器进程曾用 os.chdir() 处理每请求工作区（进程级全局状态），
多轮对话的 chdir/restore 竞态会导致进程 CWD 永久卡在已删除目录，
并发会话互踩。改为：Agent.workspace 属性为权威来源，
chat 回合入口设置 contextvar，工具/技能等深层消费方通过 get_workspace() 读取。

CLI 单进程场景下 contextvar 默认回退 Path.cwd()，行为不变。
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from contextvars import ContextVar, Token
from pathlib import Path
from typing import Iterator

_current_workspace: ContextVar[Path | None] = ContextVar("mycode_workspace", default=None)


def set_workspace(path: Path | str) -> Token:
    """设置当前上下文的工作区，返回 Token 供 reset。"""
    return _current_workspace.set(Path(path))


def reset_workspace(token: Token) -> None:
    _current_workspace.reset(token)


def get_workspace() -> Path:
    """当前工作区。未显式设置时回退进程 CWD（CLI 场景）。"""
    ws = _current_workspace.get()
    return ws if ws is not None else Path(os.getcwd())


@contextmanager
def workspace_scope(path: Path | str) -> Iterator[Path]:
    """上下文管理器形式的工作区作用域。"""
    token = set_workspace(path)
    try:
        yield Path(path)
    finally:
        reset_workspace(token)
