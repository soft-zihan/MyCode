"""Step 6: /rewind — rewind conversation turns and restore modified files."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from agents.agent import Agent
from agents.checkpoints import FileCheckpointStore, TurnBoundary
from agents.tools import execute_tool


def _make_agent(**kwargs):
    defaults = dict(model="deepseek-chat", api_base="https://example.com/v1", api_key="sk-test")
    defaults.update(kwargs)
    return Agent(**defaults)


# ─── FileCheckpointStore ────────────────────────────────────

def test_snapshot_and_restore_modified_file(tmp_path):
    store = FileCheckpointStore("sess1")
    f = tmp_path / "a.txt"
    f.write_text("original")

    store.snapshot(f)
    f.write_text("modified")

    results = store.restore_after(0)
    assert results[str(f.resolve())] == "restored"
    assert f.read_text() == "original"


def test_restore_deletes_file_created_after_boundary(tmp_path):
    store = FileCheckpointStore("sess2")
    f = tmp_path / "new.txt"
    assert not f.exists()

    store.snapshot(f)  # records "did not exist"
    f.write_text("created later")

    results = store.restore_after(0)
    assert results[str(f.resolve())] == "deleted"
    assert not f.exists()


def test_restore_only_affects_files_after_index(tmp_path):
    store = FileCheckpointStore("sess3")
    f1 = tmp_path / "f1.txt"
    f2 = tmp_path / "f2.txt"
    f1.write_text("one-v1")
    f2.write_text("two-v1")

    store.snapshot(f1)  # index 0
    f1.write_text("one-v2")
    store.snapshot(f2)  # index 1
    f2.write_text("two-v2")

    # Restore only changes after index 1 (f2), leave f1 as-is.
    results = store.restore_after(1)
    assert str(f2.resolve()) in results
    assert f2.read_text() == "two-v1"
    assert f1.read_text() == "one-v2"  # untouched


def test_snapshot_dedup_same_content(tmp_path):
    store = FileCheckpointStore("sess4")
    f = tmp_path / "d.txt"
    f.write_text("same")

    i1 = store.snapshot(f)
    i2 = store.snapshot(f)
    # Same path + same content -> no new snapshot file.
    assert i1 == i2
    assert store.checkpoint_count == 1


def test_serialization_roundtrip(tmp_path):
    store = FileCheckpointStore("sess5")
    f = tmp_path / "s.txt"
    f.write_text("v1")
    store.snapshot(f)

    data = store.to_dict()
    store2 = FileCheckpointStore("sess5")
    store2.restore_state(data)
    assert store2.checkpoint_count == 1

    f.write_text("v2")
    store2.restore_after(0)
    assert f.read_text() == "v1"


# ─── execute_tool snapshots before write ────────────────────

def test_execute_tool_snapshots_before_write(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    store = FileCheckpointStore("sess6")
    f = tmp_path / "target.txt"
    f.write_text("before")

    # Mark as read so write_file passes the read-before-write guard.
    read_state = {str(f.resolve()): f.stat().st_mtime}
    asyncio.run(execute_tool("write_file", {"file_path": str(f), "content": "after"}, read_state, store))
    assert f.read_text() == "after"
    assert store.checkpoint_count == 1

    store.restore_after(0)
    assert f.read_text() == "before"


# ─── Agent.rewind ───────────────────────────────────────────

def test_rewind_truncates_messages_and_restores_files(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    agent = _make_agent()
    f = tmp_path / "code.py"
    f.write_text("v1")

    # Simulate turn 1: boundary at 0 messages, then user+assistant messages.
    agent._turn_boundaries.append(TurnBoundary(turn=1, message_count=0, checkpoint_count=0))
    agent._openai_messages.extend([
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
    ])

    # Simulate turn 2: boundary at 2 messages; agent modifies the file.
    agent._turn_boundaries.append(TurnBoundary(turn=2, message_count=2, checkpoint_count=0))
    agent._checkpoint_store.snapshot(f)
    f.write_text("v2")
    agent._openai_messages.extend([
        {"role": "user", "content": "edit file"},
        {"role": "assistant", "content": "done"},
    ])

    result = agent.rewind(1)
    assert "Rewound 1 turn" in result
    assert len(agent._openai_messages) == 2
    assert f.read_text() == "v1"
    assert len(agent._turn_boundaries) == 1


def test_rewind_deletes_newly_created_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    agent = _make_agent()
    f = tmp_path / "created.txt"

    agent._turn_boundaries.append(TurnBoundary(turn=1, message_count=0, checkpoint_count=0))
    agent._openai_messages.append({"role": "user", "content": "create"})

    agent._checkpoint_store.snapshot(f)  # did not exist
    f.write_text("new content")

    agent.rewind(1)
    assert not f.exists()


def test_rewind_no_turns():
    agent = _make_agent()
    assert "Nothing to rewind" in agent.rewind(1)


def test_rewind_too_many_turns():
    agent = _make_agent()
    agent._turn_boundaries.append(TurnBoundary(turn=1, message_count=0, checkpoint_count=0))
    assert "Cannot rewind 5 turns" in agent.rewind(5)


def test_sub_agent_shares_checkpoint_store():
    parent = _make_agent()
    child = parent._spawn_sub_agent(system_prompt="sp", tools=[], model_ref="", label="explore")
    assert child._checkpoint_store is parent._checkpoint_store


def test_rewind_clears_read_file_state(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    agent = _make_agent()
    f = tmp_path / "r.txt"
    f.write_text("v1")
    key = str(f.resolve())
    agent._read_file_state[key] = 123.0

    agent._turn_boundaries.append(TurnBoundary(turn=1, message_count=0, checkpoint_count=0))
    agent._checkpoint_store.snapshot(f)
    f.write_text("v2")

    agent.rewind(1)
    assert key not in agent._read_file_state
