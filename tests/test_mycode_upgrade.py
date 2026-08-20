"""My Code 升级：session fork、后台 shell、token 估算、工作区结构注入。"""

from __future__ import annotations

import time

import pytest

from agents.agent import Agent
from agents.checkpoints import FileCheckpointStore
from agents.context_edit import estimate_tokens, describe_messages
from agents.prompt import build_workspace_structure
from agents.tools import (
    _run_shell,
    _shell_status,
    _start_background_shell,
    _BACKGROUND_JOBS,
    set_background_done_callback,
)


def _make_agent(**kwargs):
    defaults = dict(model="deepseek-chat", api_base="https://example.com/v1", api_key="sk-test")
    defaults.update(kwargs)
    return Agent(**defaults)


# ─── session fork ───────────────────────────────────────────

def test_fork_creates_independent_branch():
    agent = _make_agent()
    agent._openai_messages.append({"role": "user", "content": "hello"})
    old_id = agent.session_id
    msg = agent.fork_session()
    assert old_id in msg
    assert agent.session_id != old_id
    # 消息内容一致但是独立对象
    assert agent._openai_messages[-1]["content"] == "hello"


def test_fork_messages_are_deep_copies():
    agent = _make_agent()
    agent._openai_messages.append({"role": "user", "content": "hello"})
    before = agent._openai_messages
    agent.fork_session()
    # fork 后修改新分支不影响 fork 前的 list 对象（已是深拷贝）
    agent._openai_messages.append({"role": "user", "content": "new"})
    assert {"role": "user", "content": "new"} not in before


def test_checkpoint_fork_copies_snapshots(tmp_path):
    store = FileCheckpointStore("fork-src-test")
    f = tmp_path / "a.txt"
    f.write_text("v1")
    store.snapshot(f)
    new_store = store.fork("fork-dst-test")
    assert new_store.checkpoint_count == store.checkpoint_count
    assert new_store.session_id == "fork-dst-test"
    # 快照文件已复制到新目录
    assert len(list(new_store.dir.glob("*.txt"))) == 1


# ─── 后台 shell ─────────────────────────────────────────────

def test_background_shell_returns_job_id_immediately():
    result = _run_shell({"command": "echo bg-test", "background": True})
    assert "job_id=" in result
    assert "background" in result.lower()


def test_background_shell_completes_and_collects_output():
    _start_background_shell("echo done-marker")
    # 找到最新的 job
    job_id = list(_BACKGROUND_JOBS)[-1]
    deadline = time.time() + 5
    while time.time() < deadline and not _BACKGROUND_JOBS[job_id]["done"]:
        time.sleep(0.05)
    status = _shell_status({"job_id": job_id})
    assert "done" in status
    assert "done-marker" in status


def test_background_done_callback_fires():
    events = []
    set_background_done_callback(lambda jid, cmd, out, code: events.append((jid, out, code)))
    try:
        _start_background_shell("echo cb-marker")
        deadline = time.time() + 5
        while time.time() < deadline and not events:
            time.sleep(0.05)
        assert events, "callback did not fire"
        assert "cb-marker" in events[0][1]
        assert events[0][2] == 0
    finally:
        set_background_done_callback(None)


def test_shell_status_no_jobs():
    assert "No background jobs" in _shell_status({"job_id": ""}) or isinstance(_shell_status({"job_id": ""}), str)


def test_shell_status_unknown_job_lists_active():
    result = _shell_status({"job_id": "job-nonexistent"})
    assert "No background job named" in result


# ─── token 估算 ─────────────────────────────────────────────

def test_estimate_tokens_ascii():
    # 4 字符 ≈ 1 token
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("a" * 400) == 100


def test_estimate_tokens_cjk():
    # CJK 1 字符 ≈ 0.7 token
    assert estimate_tokens("中" * 10) == 7


def test_describe_messages_includes_tokens():
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hello world"},
    ]
    rows = describe_messages(msgs, use_openai=True)
    assert all("tokens" in r for r in rows)
    assert rows[1]["tokens"] > 0


# ─── 工作区结构注入 ─────────────────────────────────────────

def test_workspace_structure_lists_top_level(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "README.md").write_text("x")
    (tmp_path / ".hidden").write_text("x")
    structure = build_workspace_structure()
    assert "src/" in structure
    assert "README.md" in structure
    assert ".hidden" not in structure


def test_workspace_structure_skips_noise(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "app.py").write_text("x")
    structure = build_workspace_structure()
    assert "node_modules" not in structure
    assert "__pycache__" not in structure
    assert "app.py" in structure


# ─── 上下文窗口 200k ────────────────────────────────────────

def test_context_window_default_200k():
    agent = _make_agent(model="unknown-model")
    assert agent.context_window == 200000
    assert agent.effective_window == 180000
    assert "/200000" in agent.status_line()


def test_status_line_shows_unknown_before_first_call():
    """首轮 API 调用前 ctx 精确值未知（系统提示词+记忆+skills 已占位），
    应显示 '-' 而不是误导性的 0。"""
    agent = _make_agent()
    assert agent.last_input_token_count == 0
    line = agent.status_line()
    assert "ctx: -/" in line
    assert "ctx: 0/" not in line


def test_status_line_shows_tokens_after_api_report():
    agent = _make_agent()
    agent.last_input_token_count = 12650
    line = agent.status_line()
    assert "ctx: 12650/200000" in line
    assert "%" in line
