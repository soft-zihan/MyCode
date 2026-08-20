"""Step 9: ACE-style reversible context orchestration (minimal viable)."""

from __future__ import annotations

import pytest

from agents.agent import Agent
from agents.context_store import (
    ContextStore,
    cleared_placeholder,
    is_compressed_placeholder,
    make_abstract,
    snipped_placeholder,
)


def _make_agent(**kwargs):
    defaults = dict(model="deepseek-chat", api_base="https://example.com/v1", api_key="sk-test")
    defaults.update(kwargs)
    return Agent(**defaults)


# ─── ContextStore core ──────────────────────────────────────

def test_store_and_recover_raw_losslessly():
    store = ContextStore()
    raw = "x" * 5000 + "tail-marker"
    store.store("k1", raw)
    assert store.get_raw("k1") == raw  # lossless


def test_store_is_idempotent():
    store = ContextStore()
    store.store("k1", "first")
    store.store("k1", "second")
    assert store.get_raw("k1") == "first"


def test_abstract_is_head_tail_preview():
    content = "HEAD" + "y" * 1000 + "TAIL"
    abstract = make_abstract(content)
    assert abstract.startswith("HEAD")
    assert abstract.endswith("TAIL")
    assert "omitted" in abstract


def test_dropped_entries_not_recoverable():
    store = ContextStore()
    store.store("k1", "data")
    store.mark_dropped("k1")
    assert store.get_raw("k1") is None
    assert store.is_dropped("k1")
    assert "k1" not in store.available_keys()


def test_serialization_roundtrip():
    store = ContextStore()
    store.store("k1", "data-one")
    store.store("k2", "data-two")
    store.mark_dropped("k2")

    store2 = ContextStore()
    store2.restore_state(store.to_dict())
    assert store2.get_raw("k1") == "data-one"
    assert store2.get_raw("k2") is None


def test_placeholder_detection():
    assert is_compressed_placeholder(snipped_placeholder("k"))
    assert is_compressed_placeholder(cleared_placeholder("k"))
    assert not is_compressed_placeholder("normal content")
    assert not is_compressed_placeholder(None)


def test_placeholder_carries_key():
    p = snipped_placeholder("snip:abc")
    assert "snip:abc" in p
    assert "context_restore" in p


# ─── snip reversibility (OpenAI path) ───────────────────────

def _openai_with_tool_results(n: int):
    msgs = [{"role": "system", "content": "sys"}]
    for i in range(n):
        msgs.append({"role": "assistant", "content": None, "tool_calls": [
            {"id": f"call_{i}", "type": "function", "function": {"name": "run_shell", "arguments": "{}"}},
        ]})
        msgs.append({"role": "tool", "tool_call_id": f"call_{i}", "content": f"result-{i}-" + "z" * 100})
    return msgs


def test_snip_stores_original_and_placeholder_has_key():
    agent = _make_agent()
    agent._openai_messages = _openai_with_tool_results(6)
    agent.last_input_token_count = int(agent.effective_window * 0.7)  # above SNIP_THRESHOLD
    agent._snip_stale_results_openai()

    # oldest results snipped, recent kept
    snipped = [m for m in agent._openai_messages if m.get("role") == "tool"
               and is_compressed_placeholder(m.get("content", ""))]
    assert len(snipped) == 3  # 6 - KEEP_RECENT_RESULTS

    # originals recoverable losslessly
    for i in range(3):
        key = f"snip:call_{i}"
        raw = agent._context_store.get_raw(key)
        assert raw == f"result-{i}-" + "z" * 100
        assert key in snipped[i]["content"]


def test_context_restore_tool_recovers_snipped():
    agent = _make_agent()
    agent._context_store.store("snip:call_0", "the original data")
    out = agent._execute_context_restore_tool({"key": "snip:call_0"})
    assert out == "the original data"


def test_context_restore_tool_unknown_key_lists_available():
    agent = _make_agent()
    agent._context_store.store("snip:call_9", "data")
    out = agent._execute_context_restore_tool({"key": "nope"})
    assert "Error" in out
    assert "snip:call_9" in out


# ─── microcompact reversibility ─────────────────────────────

def test_microcompact_stores_original():
    import time as _time

    agent = _make_agent()
    agent._openai_messages = _openai_with_tool_results(6)
    agent.last_api_call_time = _time.time() - 10 * 60  # idle > 5 min
    agent._microcompact_openai()

    cleared = [m for m in agent._openai_messages if m.get("role") == "tool"
               and is_compressed_placeholder(m.get("content", ""))]
    assert len(cleared) == 3
    assert agent._context_store.get_raw("clear:call_0") == "result-0-" + "z" * 100


# ─── /ctx del persistent drop ───────────────────────────────

def test_ctx_del_drops_referenced_store_entries():
    agent = _make_agent()
    agent._openai_messages = _openai_with_tool_results(6)
    agent.last_input_token_count = int(agent.effective_window * 0.7)
    agent._snip_stale_results_openai()
    assert agent._context_store.get_raw("snip:call_0") is not None

    # Find the index of the snipped tool message and delete its group.
    idx = next(i for i, m in enumerate(agent._openai_messages)
               if m.get("role") == "tool" and "snip:call_0" in str(m.get("content")))
    agent.delete_context_messages([idx])
    # The entry is now persistently dropped.
    assert agent._context_store.get_raw("snip:call_0") is None
    assert agent._context_store.is_dropped("snip:call_0")


def test_ctx_del_keeps_unrelated_entries():
    agent = _make_agent()
    agent._context_store.store("snip:keepme", "survivor")
    agent._openai_messages = _openai_with_tool_results(2)
    agent.delete_context_messages([1])  # delete the user-less pair? index 1 is assistant
    assert agent._context_store.get_raw("snip:keepme") == "survivor"


# ─── session persistence ────────────────────────────────────

def test_context_store_persists_in_session(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from agents.session import load_session

    agent = _make_agent()
    agent._openai_messages = [{"role": "system", "content": "s"}]
    agent._context_store.store("snip:x", "persisted-raw")
    agent._auto_save()

    data = load_session(agent.session_id)
    assert data["contextStore"]["entries"]["snip:x"]["raw"] == "persisted-raw"

    # A fresh agent restoring the session gets the store back.
    agent2 = _make_agent()
    agent2.restore_session(data)
    assert agent2._context_store.get_raw("snip:x") == "persisted-raw"
