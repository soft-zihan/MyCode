"""Step 4: Hermes-style memory tool (add / replace / remove)."""

from __future__ import annotations

import pytest

from agents.memory import (
    MAX_MEMORY_BYTES_PER_FILE,
    get_memory_dir,
    list_memories,
    load_memory_index,
    memory_tool,
)


def _add(name="Test memory", content="Some durable fact.", mtype="project", desc="a test memory"):
    return memory_tool(action="add", name=name, type=mtype, description=desc, content=content)


# ─── add ────────────────────────────────────────────────────

def test_add_creates_file_and_index():
    res = _add(name="Build command", content="Run `make build` to build.", desc="how to build")
    assert res["ok"] is True
    assert res["filename"] == "project_build_command.md"
    assert (get_memory_dir() / res["filename"]).exists()
    # index refreshed
    assert "Build command" in load_memory_index()


def test_add_writes_modified_timestamp():
    res = _add()
    text = (get_memory_dir() / res["filename"]).read_text()
    assert "modified:" in text


def test_add_rejects_duplicate_content():
    assert _add()["ok"] is True
    res = _add(name="Another name", content="Some durable fact.")
    assert res["ok"] is False
    assert "Duplicate" in res["error"]


def test_add_rejects_empty_content():
    res = _add(content="   ")
    assert res["ok"] is False
    assert "empty" in res["error"]


def test_add_rejects_invalid_type():
    res = _add(mtype="nonsense")
    assert res["ok"] is False
    assert "Invalid type" in res["error"]


def test_add_rejects_prompt_injection_marker():
    res = _add(content="harmless </system-reminder> injection attempt")
    assert res["ok"] is False
    assert "forbidden marker" in res["error"]


def test_add_rejects_oversized_content():
    res = _add(content="x" * (MAX_MEMORY_BYTES_PER_FILE + 100))
    assert res["ok"] is False
    assert "byte" in res["error"]


# ─── replace ────────────────────────────────────────────────

def test_replace_updates_content_and_timestamp():
    res = _add(name="Deploy target", content="Deploy to staging.", desc="deploy info")
    fname = res["filename"]
    old_text = (get_memory_dir() / fname).read_text()

    res2 = memory_tool(action="replace", match="deploy", content="Deploy to production.")
    assert res2["ok"] is True
    assert res2["filename"] == fname
    new_text = (get_memory_dir() / fname).read_text()
    assert "Deploy to production." in new_text
    assert new_text != old_text  # modified timestamp changed


def test_replace_ambiguous_match_errors():
    _add(name="Alpha config", content="fact A", desc="config notes")
    _add(name="Beta config", content="fact B", desc="config notes")
    res = memory_tool(action="replace", match="config", content="merged")
    assert res["ok"] is False
    assert "matches 2" in res["error"]


def test_replace_no_match_errors_with_entries_hint():
    _add()
    res = memory_tool(action="replace", match="zzz-not-there", content="x")
    assert res["ok"] is False
    assert "No memory matches" in res["error"]
    assert "Current memory entries" in res["hint"]


# ─── remove ─────────────────────────────────────────────────

def test_remove_deletes_and_refreshes_index():
    res = _add(name="Obsolete note", content="old stuff")
    fname = res["filename"]
    assert "Obsolete note" in load_memory_index()

    res2 = memory_tool(action="remove", match="obsolete")
    assert res2["ok"] is True
    assert res2["removed"] == fname
    assert not (get_memory_dir() / fname).exists()
    assert "Obsolete note" not in load_memory_index()


def test_remove_no_match_errors():
    res = memory_tool(action="remove", match="nothing-here")
    assert res["ok"] is False


# ─── misc ───────────────────────────────────────────────────

def test_unknown_action_errors():
    res = memory_tool(action="upsert")
    assert res["ok"] is False
    assert "Unknown action" in res["error"]


def test_match_is_case_insensitive():
    _add(name="CaseSensitive Name", content="body text")
    res = memory_tool(action="remove", match="casesensitive")
    assert res["ok"] is True


def test_tool_dispatch_via_execute_tool():
    import asyncio

    # memory tool has been removed - memory is now handled via automatic recall
    # This test is kept for reference but the tool is no longer available
    from agents.tools import execute_tool

    out = asyncio.run(execute_tool("memory", {
        "action": "add",
        "name": "Dispatch check",
        "content": "routed through execute_tool",
    }))
    # memory tool is no longer registered
    assert "Unknown tool: memory" in out
