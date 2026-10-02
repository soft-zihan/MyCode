"""BC-4：工具运行时异常反馈给模型，不再杀死 turn。

eval bad case：grep 工具 subprocess.TimeoutExpired 穿透 dispatcher → turn/end reason=error。
"""

from __future__ import annotations

import asyncio
import json
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


# ── detect_failure：结构化载荷里「明确为空」的 error 字段不是失败信号 ──────────
#
# 回归来源（真机端到端烟测抓到）：task_list 的摘要视图刻意带 `error` 字段（failed 条
# 的原因，其余时候是空串），于是每次成功的清单更新都回显 `"error": ""`，子串扫描命中
# "error" → outcome 落成 error、`_tool_error_streak` 递增。而 error streak 是**写进
# 提示词**的（prompt_runtime 的 "Recent tool error streak"），所以一场完全正常的计划
# 执行里，模型每流转一次任务状态就被多告知一次「你最近连续失败」。实测那轮 4 次
# task_list 全部 outcome=error，而同批的 run_shell 是 success。
# 此前测不出来，是因为跑 task_list 结果的那套 harness 把 looks_like_tool_failure
# 桩成了常量 False（见 test_task_tools.py 的 FakeAgent）。

from agents.tools.executor import detect_failure  # noqa: E402
from agents.tools.task_store import TaskItem  # noqa: E402
from agents.tools.task_tools import summary_dict  # noqa: E402


def _wrapped(tool: str, payload: str) -> str:
    return f'<tool_result tool="{tool}">\n{payload}\n</tool_result>'


def test_task_list_summary_with_empty_error_field_is_not_a_failure():
    """靶心：用**真正的生产者** summary_dict 造载荷，别手写一份会长得不一样的 JSON。"""
    item = TaskItem(id=1, content="utils.py 追加 clamp 函数", status="in_progress",
                    acceptance='python -c "from utils import clamp"')
    payload = _wrapped("task_list", json.dumps(
        {"ok": True, "action": "updated", "task": summary_dict(item)},
        ensure_ascii=False, indent=2))
    assert '"error": ""' in payload  # 前提自检：键真在，否则这条测试什么都没钉住
    assert detect_failure("task_list", payload, payload) is False


def test_non_empty_error_value_is_still_a_failure():
    """摘的只是**空值**，不是键名——否则就把真失败一起摘掉了（新的假阴性）。"""
    payload = _wrapped("some_tool", '{"ok": false, "error": "boom"}')
    assert detect_failure("some_tool", payload, payload) is True


def test_error_as_a_value_under_another_key_is_still_a_failure():
    payload = _wrapped("some_tool", '{"status": "error", "detail": "x"}')
    assert detect_failure("some_tool", payload, payload) is True


def test_prose_failure_is_still_a_failure():
    """自由文本这条路一个字没变：真实报错从来不写成 `"error": ""`。"""
    for text in ("Error: command not found", "permission denied",
                 "operation timed out after 30s"):
        assert detect_failure("run_shell", text, text) is True, text


def test_empty_denied_and_timeout_fields_are_not_failures():
    payload = _wrapped("some_tool", '{"denied": null, "timeout": "", "items": []}')
    assert detect_failure("some_tool", payload, payload) is False
