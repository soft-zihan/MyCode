"""中止时把正在跑的前台命令杀掉（按 owner 分组，不误伤并发子 Agent）。

机制背景：`run_shell` 是同步函数、经 `asyncio.to_thread` 跑在线程里，
**asyncio 取消打不断线程**。所以用户点「停止」之后，turn 会闭合、事件流会写
`turn/end{aborted}`、`turn/cancel{confirmed:true}`，而命令还在跑——2026-10-28
实测：abort 返回后 1.5s，`sleep 30` 仍然存活。这正是「传输结果 ≠ 执行结果」
最具体的一个例子：回执说停了，进程说没停。
"""

from __future__ import annotations

import threading
import time

import pytest

from agents.tools.runtime import (
    LocalRuntime,
    kill_inflight_commands,
    set_command_owner,
)


@pytest.fixture(autouse=True)
def _workspace(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    yield tmp_path


def _run_in_thread(owner: int, command: str, timeout_s: float = 60) -> tuple[list, threading.Thread]:
    """在带 owner 的上下文里起一条命令。

    contextvar 必须在**线程内**设置：真实路径是 _call_tool_handler 在 to_thread
    之前设，由 to_thread 拷贝 context 带进线程；这里直接在线程里设，等价。
    """
    out: list = []

    def body():
        set_command_owner(owner)
        out.append(LocalRuntime().run_command(command, timeout_s))

    t = threading.Thread(target=body, daemon=True)
    t.start()
    return out, t


def test_kill_stops_a_running_command():
    out, t = _run_in_thread(1001, "sleep 30")
    time.sleep(0.6)                       # 让它真的起来并登记

    t0 = time.monotonic()
    killed = kill_inflight_commands(1001)
    t.join(timeout=15)
    elapsed = time.monotonic() - t0

    assert killed == 1, "登记表里没找到这条命令——owner 没对上"
    assert not t.is_alive(), "命令没被杀掉，线程还阻塞在 communicate()"
    assert elapsed < 10, f"杀掉花了 {elapsed:.1f}s，不像 SIGKILL"
    exit_code = out[0][0]
    assert exit_code != 0, f"被杀的命令不该是 0 退出，实际 {exit_code}"


def test_kill_does_not_touch_another_owner():
    """并发子 Agent 各跑各的命令：中止一个不能顺手杀掉另一个的。"""
    _, ta = _run_in_thread(2001, "sleep 30")
    _, tb = _run_in_thread(2002, "sleep 30")
    time.sleep(0.6)

    assert kill_inflight_commands(2001) == 1
    ta.join(timeout=15)
    assert not ta.is_alive()

    # B 还活着 = 它仍在登记表里，杀得掉
    assert kill_inflight_commands(2002) == 1, "A 的中止把 B 的命令也干掉了"
    tb.join(timeout=15)
    assert not tb.is_alive()


def test_registry_is_clean_after_normal_completion():
    out, t = _run_in_thread(3001, "echo ok")
    t.join(timeout=15)
    assert out and out[0][0] == 0
    assert kill_inflight_commands(3001) == 0, "正常跑完的命令没从登记表里摘掉，会越积越多"


def test_kill_unknown_owner_is_a_noop():
    assert kill_inflight_commands(999999) == 0
