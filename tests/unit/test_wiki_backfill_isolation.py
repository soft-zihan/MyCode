"""BC-6：backfill 工作区隔离 + 水位线容错 + 评测 workspace 保留清单。"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from agents.core.workspace import workspace_scope
from agents.wiki.wiki_capture import (
    find_sessions_needing_compilation,
    get_session_workspace,
    mark_session_compiled,
)


@pytest.fixture
def ws(tmp_path, monkeypatch):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path))
    with workspace_scope(workspace):
        yield workspace


def _write_session(tmp_home: Path, session_id: str, cwd: str | None, max_seq: int, age_s: float = 3600):
    sdir = tmp_home / ".mycode" / "sessions"
    sdir.mkdir(parents=True, exist_ok=True)
    path = sdir / f"{session_id}.events.jsonl"
    lines = []
    seq = 0
    if cwd is not None:
        lines.append(json.dumps({"type": "session/created", "session_id": session_id, "cwd": cwd, "seq": seq}))
    for i in range(max_seq):
        seq += 1
        lines.append(json.dumps({"type": "user_message", "session_id": session_id, "content": f"m{i}", "seq": seq}))
    path.write_text("\n".join(lines) + "\n")
    old = time.time() - age_s
    os.utime(path, (old, old))
    return path


def test_get_session_workspace_reads_cwd(ws, tmp_path):
    _write_session(tmp_path, "s-in", str(ws), 3)
    assert get_session_workspace("s-in") == str(ws.resolve())


def test_get_session_workspace_missing_created_event(ws, tmp_path):
    _write_session(tmp_path, "s-nocwd", None, 3)
    assert get_session_workspace("s-nocwd") is None


def test_backfill_only_current_workspace_sessions(ws, tmp_path):
    """BC-6b：全局 session 存储中其它工作区的会话不得进入补编译清单。"""
    _write_session(tmp_path, "s-in", str(ws), 30)
    _write_session(tmp_path, "s-other", str(tmp_path / "elsewhere"), 30)
    _write_session(tmp_path, "s-nocwd", None, 30)

    needing = find_sessions_needing_compilation(threshold=10)
    ids = {s[0] for s in needing}
    assert ids == {"s-in"}


def test_backfill_skips_recent_sessions(ws, tmp_path):
    _write_session(tmp_path, "s-busy", str(ws), 30, age_s=1)
    assert find_sessions_needing_compilation(threshold=10) == []


def test_mark_session_compiled_missing_file_no_raise(ws, tmp_path):
    """BC-6：segment 被并发清理（评测 wipe）后 mark 应跳过而非抛错。"""
    missing = ws / ".mycode" / "wiki" / "session" / "gone_seg1.md"
    mark_session_compiled(missing)  # 不抛异常即通过


def test_wipe_except_keeps_whitelist(tmp_path):
    """BC-5/6：评测 workspace 清空须保留 .embed-cache 与 .extract_state.json。"""
    from eval.smoke.runner import _wipe_except

    ws = tmp_path / "ws"
    wiki = ws / ".mycode" / "wiki"
    (wiki / ".embed-cache").mkdir(parents=True)
    (wiki / ".embed-cache" / "vec.bin").write_bytes(b"x")
    (wiki / ".extract_state.json").write_text("{}")
    (wiki / "knowledge").mkdir()
    (wiki / "knowledge" / "a.md").write_text("a")
    (ws / "utils.py").write_text("u")
    (ws / "stale-dir" / "nested").mkdir(parents=True)
    (ws / "stale-dir" / "nested" / "f.txt").write_text("f")

    keep = {wiki / ".embed-cache", wiki / ".extract_state.json"}
    _wipe_except(ws, keep)

    assert (wiki / ".embed-cache" / "vec.bin").exists()
    assert (wiki / ".extract_state.json").exists()
    assert not (wiki / "knowledge").exists()
    assert not (ws / "utils.py").exists()
    assert not (ws / "stale-dir").exists()
    assert ws.exists() and wiki.exists()
