"""显式工作区重构回归测试。

核心保障：
- 服务器进程 CWD 不随请求切换；会话工作区经 Agent.workspace + workspace contextvar 显式传递
- 工具相对路径锚定会话工作区，而非进程 CWD
- skills / custom agents 缓存按工作区键控，多工作区互不串台
- SessionManager.create 不再 chdir；cwd 进入事件流（session/created）→ 投影 → 重建
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from agents.core.workspace import (
    get_workspace,
    reset_workspace,
    set_workspace,
    workspace_scope,
)


# ─── contextvar 基本行为 ─────────────────────────────────────────────

def test_get_workspace_falls_back_to_process_cwd(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    assert get_workspace().resolve() == tmp_path.resolve()


def test_set_and_reset_workspace(tmp_path):
    original = get_workspace()
    token = set_workspace(tmp_path)
    try:
        assert get_workspace() == tmp_path
    finally:
        reset_workspace(token)
    assert get_workspace().resolve() == original.resolve()


def test_workspace_scope_restores_on_exception(tmp_path):
    original = get_workspace()
    with pytest.raises(RuntimeError):
        with workspace_scope(tmp_path):
            assert get_workspace() == tmp_path
            raise RuntimeError("boom")
    assert get_workspace().resolve() == original.resolve()


def test_workspace_scope_nesting(tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    with workspace_scope(a):
        assert get_workspace() == a
        with workspace_scope(b):
            assert get_workspace() == b
        assert get_workspace() == a


# ─── 工具路径锚定 ───────────────────────────────────────────────────

def test_relative_path_anchors_to_workspace_not_process_cwd(monkeypatch, tmp_path):
    """核心回归：进程 CWD ≠ 会话工作区时，相对路径必须锚定会话工作区。"""
    from agents.tools.paths import resolve_tool_path

    ws = tmp_path / "ws"
    other = tmp_path / "other"
    ws.mkdir()
    other.mkdir()
    monkeypatch.chdir(other)
    with workspace_scope(ws):
        resolved = resolve_tool_path("foo.txt", must_exist=False)
    assert resolved == ws / "foo.txt"


def test_absolute_existing_path_passthrough(tmp_path):
    from agents.tools.paths import resolve_tool_path

    f = tmp_path / "real.txt"
    f.write_text("x")
    assert resolve_tool_path(str(f)) == f


def test_stale_absolute_path_rerooted_to_workspace(tmp_path):
    from agents.tools.paths import resolve_tool_path

    ws = tmp_path / "ws"
    (ws / "src").mkdir(parents=True)
    (ws / "src" / "app.py").write_text("print(1)")
    with workspace_scope(ws):
        resolved = resolve_tool_path("/nonexistent-prefix-xyz/src/app.py")
    assert resolved == ws / "src" / "app.py"


def test_workspace_guard_inside_and_outside(tmp_path):
    from agents.tools.paths import workspace_guard_error

    ws = tmp_path / "ws"
    outside = tmp_path / "outside"
    ws.mkdir()
    outside.mkdir()
    with workspace_scope(ws):
        assert workspace_guard_error(ws / "sub") is None
        err = workspace_guard_error(outside)
        assert err is not None
        assert "outside the workspace" in err


# ─── LocalRuntime 锚定 ──────────────────────────────────────────────

def test_local_runtime_command_runs_in_workspace(tmp_path):
    from agents.tools.runtime import LocalRuntime

    ws = tmp_path / "ws"
    ws.mkdir()
    rt = LocalRuntime()
    with workspace_scope(ws):
        code, out, _ = rt.run_command("pwd")
    assert code == 0
    assert Path(out.strip()).resolve() == ws.resolve()


def test_local_runtime_list_and_grep_anchor_to_workspace(tmp_path):
    from agents.tools.runtime import LocalRuntime

    ws = tmp_path / "ws"
    (ws / "sub").mkdir(parents=True)
    (ws / "sub" / "a.txt").write_text("hello-workspace-marker")
    rt = LocalRuntime()
    with workspace_scope(ws):
        files = rt.list_files("sub", "*.txt")
        hits = rt.grep_search("hello-workspace-marker", "sub")
    assert files == ["a.txt"]
    assert "hello-workspace-marker" in hits


# ─── 缓存按工作区键控 ────────────────────────────────────────────────

def _write_custom_agent(ws: Path, name: str) -> None:
    d = ws / ".mycode" / "agents"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.md").write_text(
        f"---\nname: {name}\ndescription: test agent\n---\nPrompt body.\n"
    )


def _write_skill(ws: Path, name: str) -> None:
    d = ws / ".mycode" / "skills" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(f"---\ndescription: test skill {name}\n---\nBody.\n")


def test_custom_agents_cache_keyed_by_workspace(tmp_path):
    import agents.core.subagent as subagent_mod

    ws_a = tmp_path / "a"
    ws_b = tmp_path / "b"
    _write_custom_agent(ws_a, "agent-alpha-ws")
    _write_custom_agent(ws_b, "agent-beta-ws")
    subagent_mod.reset_agent_cache()
    try:
        with workspace_scope(ws_a):
            agents_a = subagent_mod._discover_custom_agents()
            assert "agent-alpha-ws" in agents_a
            assert "agent-beta-ws" not in agents_a
        with workspace_scope(ws_b):
            agents_b = subagent_mod._discover_custom_agents()
            assert "agent-beta-ws" in agents_b
            assert "agent-alpha-ws" not in agents_b
        # 缓存命中不串台
        with workspace_scope(ws_a):
            assert "agent-alpha-ws" in subagent_mod._discover_custom_agents()
    finally:
        subagent_mod.reset_agent_cache()


def test_skills_cache_keyed_by_workspace(tmp_path):
    import agents.skills.skills as skills_mod

    ws_a = tmp_path / "a"
    ws_b = tmp_path / "b"
    _write_skill(ws_a, "skill-alpha-ws")
    _write_skill(ws_b, "skill-beta-ws")
    skills_mod.reset_skill_cache()
    try:
        with workspace_scope(ws_a):
            names_a = {s.name for s in skills_mod.discover_skills()}
            assert "skill-alpha-ws" in names_a
            assert "skill-beta-ws" not in names_a
        with workspace_scope(ws_b):
            names_b = {s.name for s in skills_mod.discover_skills()}
            assert "skill-beta-ws" in names_b
            assert "skill-alpha-ws" not in names_b
    finally:
        skills_mod.reset_skill_cache()


# ─── Agent / SessionManager ─────────────────────────────────────────

def _isolate_agent_env(monkeypatch, tmp_path) -> Path:
    """隔离 Agent 构造的磁盘副作用：session 存储目录。"""
    import agents.core.session as session_mod

    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    monkeypatch.setenv("MYCODE_SESSION_DIR", str(sessions_dir))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(session_mod, "_backend", None)
    return sessions_dir


def test_agent_init_sets_workspace_without_leaking_contextvar(monkeypatch, tmp_path):
    from agents.agent import Agent

    _isolate_agent_env(monkeypatch, tmp_path)
    ws = tmp_path / "ws"
    ws.mkdir()
    original = get_workspace()
    agent = Agent(model="m", api_key="sk-test", api_base="https://x.example/v1", workspace=ws)
    assert agent.workspace == ws.resolve()
    # init 内部的 workspace token 必须已复位，不泄漏到调用方上下文
    assert get_workspace().resolve() == original.resolve()


def test_create_keeps_process_cwd_and_records_workspace_event(monkeypatch, tmp_path):
    """核心回归：create() 不再 os.chdir；cwd 进事件流 → 投影；进程 CWD 恒定。"""
    from agents.session_manager import SessionManager

    sessions_dir = _isolate_agent_env(monkeypatch, tmp_path)
    launch = tmp_path / "launch"
    ws = tmp_path / "ws"
    launch.mkdir()
    ws.mkdir()
    monkeypatch.chdir(launch)

    mgr = SessionManager()
    agent, session = mgr.create(model="ws-test-model", cwd=str(ws))

    assert Path(os.getcwd()).resolve() == launch.resolve()
    assert agent.workspace == ws.resolve()
    created = [e for e in session._log if e["type"] == "session/created"]
    assert len(created) == 1
    assert created[0]["cwd"] == str(ws)
    assert session.projections.get("cwd") == str(ws)
    # 事件已落盘（重建的数据源）
    assert any(sessions_dir.glob("*.events.jsonl"))


async def test_restore_recovers_workspace_from_events(monkeypatch, tmp_path):
    """重启重建：全新 manager 从事件流恢复 agent.workspace 与 cwd 投影。"""
    from agents.session_manager import SessionManager

    _isolate_agent_env(monkeypatch, tmp_path)
    launch = tmp_path / "launch"
    ws = tmp_path / "ws"
    launch.mkdir()
    ws.mkdir()
    monkeypatch.chdir(launch)

    mgr1 = SessionManager()
    _, session = mgr1.create(model="ws-test-model", cwd=str(ws))
    sid = session.id

    mgr2 = SessionManager()
    result = await mgr2.restore(sid)
    assert result is not None
    agent2, session2 = result
    assert agent2.workspace == ws.resolve()
    assert session2.projections.get("cwd") == str(ws)
    assert Path(os.getcwd()).resolve() == launch.resolve()


# ─── /api/revert 工作区解析 ─────────────────────────────────────────

def test_resolve_session_workspace_disk_and_live(monkeypatch, tmp_path):
    import frontend.server.routers.chat as chat_router
    import agents.core.session as session_mod
    import agents.core.session_projection_cache as spc
    import agents.session_manager as manager_mod
    from agents.core.session import Session

    monkeypatch.setenv("MYCODE_SESSION_DIR", str(tmp_path / "sessions"))
    monkeypatch.setattr(session_mod, "_backend", None)
    monkeypatch.setattr(spc, "_projection_cache", None)
    monkeypatch.setattr(manager_mod, "_session_manager", None)

    ws = tmp_path / "ws"
    ws.mkdir()
    sid = "ws-helper-test-session"

    disk_session = Session(session_id=sid)
    disk_session.append("session/created", {"cwd": str(ws)})

    resolved = chat_router._resolve_session_workspace(sid)
    assert resolved is not None
    assert resolved == ws.resolve()
    assert chat_router._resolve_session_workspace("no-such-session-xyz") is None

    # 活会话投影优先
    mgr = manager_mod.get_session_manager()
    live_id = sid + "-live"
    mgr._sessions[live_id] = SimpleNamespace(projections={"cwd": str(ws)})
    try:
        assert chat_router._resolve_session_workspace(live_id) == ws.resolve()
    finally:
        mgr._sessions.pop(live_id, None)
