"""BC-4：工具运行时异常反馈给模型，不再杀死 turn。

eval bad case：grep 工具 subprocess.TimeoutExpired 穿透 dispatcher → turn/end reason=error。
"""

from __future__ import annotations

import asyncio
import subprocess

import pytest

from agents.core.session import Session
from agents.tools.dispatcher import ToolDispatcher
from agents.tools.runtime import LocalRuntime


class FakeAgent:
    def __init__(self):
        self.session = Session(session_id="bc4-test", origin="test")
        self.session_id = self.session.id
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.current_sub_agent_id = None
        self.permission_mode = "bypassPermissions"

    def abort_requested(self) -> bool:
        return False


def test_dispatcher_generic_exception_returns_error_result(monkeypatch):
    dispatcher = ToolDispatcher(agent_ref=FakeAgent())

    async def boom(name, inp, assistant_seq=None):
        raise RuntimeError("injected failure")

    monkeypatch.setattr(dispatcher, "_execute_tool_call_inner", boom)
    result = asyncio.run(dispatcher.execute_tool_call("grep", {"pattern": "x"}))
    assert result.status == "error"
    assert result.outcome == "error"
    assert "injected failure" in result.text


def test_grep_search_timeout_returns_hint(monkeypatch):
    rt = LocalRuntime()

    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args[0] if args else "grep", timeout=10)

    monkeypatch.setattr(subprocess, "run", fake_run)
    out = rt.grep_search("pattern", "/tmp/some-scope")
    assert "timed out" in out
    assert "narrow the path" in out


def test_grep_search_nonzero_exit_reports(monkeypatch):
    rt = LocalRuntime()

    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(args, returncode=2, stdout="", stderr="Permission denied")

    monkeypatch.setattr(subprocess, "run", fake_run)
    out = rt.grep_search("pattern", "/tmp/x")
    assert "exited with code 2" in out
