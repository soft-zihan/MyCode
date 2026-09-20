"""Rewind / Snapshot — 当前架构测试。

职责划分：
- 文件快照：SnapshotService（git tree 快照，capture/list/inspect/restore/diff）
- 统一回退：RewindService（对话事件截断 + 文件快照恢复，stage/commit/clear 三阶段）
"""

from __future__ import annotations

import pytest

from agents.core.rewind_service import RewindService
from agents.core.session import Session
from agents.core.snapshot_service import SnapshotService


@pytest.fixture
def session_env(tmp_path, monkeypatch):
    monkeypatch.setenv("MYCODE_SESSION_DIR", str(tmp_path / "sessions"))
    import agents.core.session as sess
    import agents.core.session_projection_cache as spc

    monkeypatch.setattr(sess, "_backend", None)
    monkeypatch.setattr(spc, "_projection_cache", None)
    yield tmp_path
    monkeypatch.setattr(sess, "_backend", None)
    monkeypatch.setattr(spc, "_projection_cache", None)


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (root / "a.txt").write_text("v1")
    (root / "b.txt").write_text("b-v1")
    return root, tmp_path / "snapshots"


def _append(session: Session, type: str, **data):
    return session.append(type, data)


def _session_with_turns(n_turns: int) -> Session:
    s = Session(session_id="rewind-session")
    for i in range(n_turns):
        _append(s, "turn/start", turn=i + 1)
        _append(s, "user_message", content=f"q{i}")
        _append(s, "assistant_message", content=f"a{i}")
        _append(s, "turn/end", turn=i + 1)
    return s


# ─── SnapshotService（git 快照）─────────────────────────────

async def test_capture_and_restore_modified_file(project):
    root, snap_dir = project
    svc = SnapshotService(root, snap_dir)
    summary = await svc.capture("sess1", label="before-edit")
    assert summary.file_count == 2

    (root / "a.txt").write_text("v2")
    restored = await svc.restore(summary.id)
    assert "a.txt" in restored
    assert (root / "a.txt").read_text() == "v1"
    assert (root / "b.txt").read_text() == "b-v1"


async def test_restore_deletes_file_created_after_capture(project):
    root, snap_dir = project
    svc = SnapshotService(root, snap_dir)
    summary = await svc.capture("sess1")

    (root / "new.txt").write_text("created later")
    await svc.restore(summary.id, files=["new.txt"])
    assert not (root / "new.txt").exists()


async def test_restore_recovers_deleted_file(project):
    root, snap_dir = project
    svc = SnapshotService(root, snap_dir)
    summary = await svc.capture("sess1")

    (root / "a.txt").unlink()
    await svc.restore(summary.id, files=["a.txt"])
    assert (root / "a.txt").read_text() == "v1"


async def test_list_and_manifest(project):
    root, snap_dir = project
    svc = SnapshotService(root, snap_dir)
    s1 = await svc.capture("sess1", label="L1")
    await svc.capture("sess2", label="L2")

    all_snaps = await svc.list()
    assert len(all_snaps) == 2
    sess1 = await svc.list(session_id="sess1")
    assert [s.id for s in sess1] == [s1.id]

    manifest = await svc.get_manifest(s1.id)
    assert manifest.label == "L1"
    assert set(manifest.files) == {"a.txt", "b.txt"}


async def test_inspect_shows_pending_changes(project):
    root, snap_dir = project
    svc = SnapshotService(root, snap_dir)
    summary = await svc.capture("sess1")

    (root / "a.txt").write_text("v2")
    (root / "c.txt").write_text("new")
    inspection = await svc.inspect(summary.id)
    by_path = {f.path: f.status for f in inspection.files}
    assert by_path["a.txt"] == "modified"
    assert by_path["c.txt"] == "added"


async def test_diff_between_snapshots(project):
    root, snap_dir = project
    svc = SnapshotService(root, snap_dir)
    s1 = await svc.capture("sess1")
    (root / "a.txt").write_text("v2")
    s2 = await svc.capture("sess1")

    diffs = await svc.diff(s1.id, s2.id)
    assert len(diffs) == 1
    assert diffs[0].path == "a.txt"
    assert diffs[0].status == "modified"


# ─── RewindService（stage → commit / clear）────────────────

async def test_rewind_stage_previews_conversation_and_files(session_env, project):
    root, snap_dir = project
    snapshot_svc = SnapshotService(root, snap_dir)

    session = Session(session_id="sess1")
    _append(session, "session/created", cwd=str(root))
    _append(session, "user_message", content="first")
    _append(session, "assistant_message", content="first reply")

    (root / "a.txt").write_text("v2")
    target_snapshot = await snapshot_svc.capture("sess1", label="before second")
    _append(session, "user_message", content="second", snapshot_id=target_snapshot.id)
    _append(session, "assistant_message", content="second reply")

    (root / "a.txt").write_text("v3")
    (root / "new.txt").write_text("created later")

    svc = RewindService(snapshot_dir=snap_dir)
    plan = await svc.stage("sess1", turns=1)
    data = plan.to_dict()

    assert data["has_snapshot"] is True
    assert data["removed_user_messages"] == 1
    assert data["target_message"]["content"] == "second"
    assert {c["path"]: c["status"] for c in data["file_changes"]} == {
        "a.txt": "modified",
        "new.txt": "added",
    }
    assert (root / "a.txt").read_text() == "v3"
    assert (root / "new.txt").exists()


async def test_rewind_commit_restores_files_and_truncates_events(session_env, project):
    root, snap_dir = project
    snapshot_svc = SnapshotService(root, snap_dir)

    session = Session(session_id="sess1")
    _append(session, "session/created", cwd=str(root))
    _append(session, "user_message", content="first")
    _append(session, "assistant_message", content="first reply")

    (root / "a.txt").write_text("v2")
    target_snapshot = await snapshot_svc.capture("sess1", label="before second")
    target_seq = session.seq
    _append(session, "user_message", content="second", snapshot_id=target_snapshot.id)
    _append(session, "assistant_message", content="second reply")

    (root / "a.txt").write_text("v3")
    (root / "new.txt").write_text("created later")

    svc = RewindService(snapshot_dir=snap_dir)
    plan = await svc.stage("sess1", turns=1)
    result = await svc.commit(plan.id, session=session)

    assert (root / "a.txt").read_text() == "v2"
    assert not (root / "new.txt").exists()
    assert result["removed_user_messages"] == 1
    assert set(result["restored_files"]) == {"a.txt", "new.txt"}

    reloaded = Session.load_from_events("sess1")
    assert reloaded is not None
    persisted = [e["type"] for e in reloaded._log]
    assert persisted == [
        "session/created",
        "user_message",
        "assistant_message",
        "rewind",
    ]
    assert session.seq == target_seq + 1
    assert session._log[-1]["type"] == "rewind"
    assert session._log[-1]["target_seq"] == target_seq


async def test_rewind_without_snapshot_only_truncates_conversation(session_env, project):
    root, snap_dir = project
    session = Session(session_id="sess2")
    _append(session, "session/created", cwd=str(root))
    _append(session, "user_message", content="first")
    _append(session, "assistant_message", content="first reply")
    _append(session, "user_message", content="second")

    (root / "a.txt").write_text("v2")

    svc = RewindService(snapshot_dir=snap_dir)
    plan = await svc.stage("sess2", turns=1)
    assert plan.to_dict()["has_snapshot"] is False
    await svc.commit(plan.id, session=session)

    assert (root / "a.txt").read_text() == "v2"
    persisted = [e["type"] for e in Session.load_from_events("sess2")._log]
    assert persisted == ["session/created", "user_message", "assistant_message", "rewind"]


async def test_rewind_clear_discards_plan(session_env, project):
    root, snap_dir = project
    snapshot_svc = SnapshotService(root, snap_dir)

    session = Session(session_id="sess3")
    _append(session, "session/created", cwd=str(root))
    _append(session, "user_message", content="first")
    target_snapshot = await snapshot_svc.capture("sess3")
    _append(session, "user_message", content="second", snapshot_id=target_snapshot.id)

    (root / "a.txt").write_text("v2")

    svc = RewindService(snapshot_dir=snap_dir)
    plan = await svc.stage("sess3", turns=1)
    result = await svc.clear(plan.id)

    assert result["status"] == "cleared"
    assert (root / "a.txt").read_text() == "v2"
    assert len(session._log) == 3
    with pytest.raises(ValueError, match="not found or expired"):
        await svc.commit(plan.id)


async def test_rewind_requires_user_message(session_env):
    session = Session(session_id="sess4")
    _append(session, "assistant_message", content="orphan")

    svc = RewindService(snapshot_dir=session_env / "snapshots")
    with pytest.raises(ValueError, match="no user messages"):
        await svc.stage("sess4", turns=1)


async def test_rewind_rejects_noop(session_env):
    session = Session(session_id="sess5")
    _append(session, "user_message", content="only")

    svc = RewindService(snapshot_dir=session_env / "snapshots")
    with pytest.raises(ValueError, match="Nothing to rewind"):
        await svc.stage("sess5", keep_user_messages=1)


async def test_rewind_commit_unknown_plan_raises(session_env):
    svc = RewindService(snapshot_dir=session_env / "snapshots")
    with pytest.raises(ValueError, match="not found or expired"):
        await svc.commit("nonexistent-plan")
