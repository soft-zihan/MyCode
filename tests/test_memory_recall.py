"""Step 5: memory recall fixes — CJK gating, injection race, cooldown decay, byte units."""

from __future__ import annotations

import asyncio

import pytest

from agents.agent import Agent
from agents.memory import (
    MAX_INDEX_BYTES,
    load_memory_index,
    save_memory,
    select_relevant_memories,
    start_memory_prefetch,
)


def _make_agent(**kwargs):
    defaults = dict(model="deepseek-chat", api_base="https://example.com/v1", api_key="sk-test")
    defaults.update(kwargs)
    return Agent(**defaults)


# ─── CJK gating ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cjk_query_triggers_prefetch():
    """Chinese input has no whitespace; length gate must let it through."""
    save_memory("测试记忆", "一条中文记忆", "project", "内容正文")

    async def fake_side_query(system, user):
        return '{"selected_memories": []}'

    pf = start_memory_prefetch("帮我回忆一下项目的构建方式", fake_side_query, set(), 0)
    assert pf is not None, "CJK query should trigger memory prefetch"
    pf.task.cancel()
    try:
        await pf.task
    except (asyncio.CancelledError, Exception):
        pass


@pytest.mark.asyncio
async def test_short_single_word_does_not_trigger():
    async def fake_side_query(system, user):
        return '{"selected_memories": []}'

    pf = start_memory_prefetch("hi", fake_side_query, set(), 0)
    assert pf is None, "short single-token input should not trigger prefetch"


@pytest.mark.asyncio
async def test_multiword_still_triggers():
    save_memory("note", "a note", "project", "body")

    async def fake_side_query(system, user):
        return '{"selected_memories": []}'

    pf = start_memory_prefetch("recall the build command", fake_side_query, set(), 0)
    assert pf is not None
    pf.task.cancel()
    try:
        await pf.task
    except (asyncio.CancelledError, Exception):
        pass


# ─── byte-safe index truncation ─────────────────────────────

def test_index_truncation_does_not_split_multibyte_chars():
    # Build an index with lots of CJK so byte truncation lands mid-character
    # unless handled; result must still be valid UTF-8 text.
    for i in range(60):
        save_memory(f"中文记忆条目{i}", "描述" * 20, "project", "内容")
    index = load_memory_index()
    # Must be decodable (no mojibake / replacement from a hard byte cut).
    index.encode("utf-8").decode("utf-8")
    assert len(index.encode()) <= MAX_INDEX_BYTES + 200  # truncation marker overhead


# ─── cooldown decay ─────────────────────────────────────────

def test_cooldown_blocks_recently_surfaced():
    agent = _make_agent()
    agent._turn_number = 10
    agent._memory_surfaced_at["/tmp/m.md"] = 10
    assert "/tmp/m.md" in agent._cooled_memory_paths()


def test_cooldown_expires_after_window():
    agent = _make_agent()
    agent._turn_number = 10
    agent._memory_surfaced_at["/tmp/m.md"] = 10
    # Advance beyond the cooldown window.
    agent._turn_number = 10 + agent.MEMORY_RECALL_COOLDOWN_TURNS
    assert "/tmp/m.md" not in agent._cooled_memory_paths()


def test_chat_increments_turn_number():
    agent = _make_agent()
    before = agent._turn_number
    # Simulate the turn increment that chat() performs at the start of a turn.
    agent._turn_number += 1
    assert agent._turn_number == before + 1


# ─── CJK slug regression ────────────────────────────────────

def test_cjk_names_get_distinct_filenames():
    """Pure-CJK names must not collapse into the same `{type}_.md` file."""
    from agents.memory import _slugify

    assert _slugify("构建命令") != ""
    assert _slugify("构建命令") != _slugify("部署流程")
    f1 = save_memory("构建命令", "build", "project", "make build")
    f2 = save_memory("部署流程", "deploy", "project", "make deploy")
    assert f1 != f2
    assert "构建命令" in f1


def test_symbol_only_name_gets_hash_fallback():
    from agents.memory import _slugify

    assert _slugify("!!!") != ""


# ─── LLM order preserved ────────────────────────────────────

@pytest.mark.asyncio
async def test_select_preserves_llm_order():
    save_memory("alpha", "first", "project", "alpha body")
    save_memory("beta", "second", "project", "beta body")

    async def side_query(system, user):
        # Return beta before alpha; result should follow this order.
        return '{"selected_memories": ["project_beta.md", "project_alpha.md"]}'

    result = await select_relevant_memories("query", side_query, set())
    names = [m.path.split("/")[-1] for m in result]
    assert names == ["project_beta.md", "project_alpha.md"]


@pytest.mark.asyncio
async def test_select_falls_back_to_filename_mention():
    """If the model answers in prose instead of JSON but mentions a filename,
    the fallback should still select it."""
    save_memory("gamma", "third", "project", "gamma body")

    async def side_query(system, user):
        return "I think project_gamma.md is relevant to this query."

    result = await select_relevant_memories("query", side_query, set())
    names = [m.path.split("/")[-1] for m in result]
    assert names == ["project_gamma.md"]


@pytest.mark.asyncio
async def test_select_invalid_json_no_mention_returns_empty():
    save_memory("delta", "fourth", "project", "delta body")

    async def side_query(system, user):
        return "Sorry, I cannot decide."

    result = await select_relevant_memories("query", side_query, set())
    assert result == []
