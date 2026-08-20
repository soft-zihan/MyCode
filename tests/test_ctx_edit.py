"""Step 8: /context visualization and selective message deletion."""

from __future__ import annotations

import pytest

from agents.context_edit import (
    delete_message_group,
    describe_messages,
    keep_only_groups,
    message_group,
)


# ─── OpenAI pairing ─────────────────────────────────────────

def _openai_conversation():
    return [
        {"role": "system", "content": "You are helpful."},
        {"role": "user", "content": "read the file"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "call_1", "type": "function", "function": {"name": "read_file", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "call_1", "content": "file contents here"},
        {"role": "assistant", "content": "The file says hello."},
        {"role": "user", "content": "thanks"},
    ]


def test_openai_group_pairs_tool_calls_with_results():
    msgs = _openai_conversation()
    # assistant with tool_calls groups with its tool result
    assert message_group(msgs, 2, use_openai=True) == {2, 3}
    # tool result groups back with its owner
    assert message_group(msgs, 3, use_openai=True) == {2, 3}
    # plain messages are singletons
    assert message_group(msgs, 1, use_openai=True) == {1}


def test_openai_delete_removes_whole_pair():
    msgs = _openai_conversation()
    kept, deleted = delete_message_group(msgs, 2, use_openai=True)
    assert deleted == 2
    assert all(m.get("role") != "tool" for m in kept)
    assert not any(m.get("tool_calls") for m in kept)


def test_openai_delete_from_tool_result_side():
    msgs = _openai_conversation()
    kept, deleted = delete_message_group(msgs, 3, use_openai=True)
    assert deleted == 2
    assert len(kept) == 4


def test_openai_system_prompt_protected():
    msgs = _openai_conversation()
    kept, deleted = delete_message_group(msgs, 0, use_openai=True)
    assert deleted == 0
    assert kept[0]["role"] == "system"


def test_openai_keep_only_groups():
    msgs = _openai_conversation()
    kept, deleted = keep_only_groups(msgs, [1], use_openai=True)
    # system prompt kept + the user message group
    roles = [m["role"] for m in kept]
    assert roles == ["system", "user"]
    assert deleted == 4


# ─── Anthropic pairing ──────────────────────────────────────

def _anthropic_conversation():
    return [
        {"role": "user", "content": "read the file"},
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "tu_1", "name": "read_file", "input": {}},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "tu_1", "content": "file contents"},
        ]},
        {"role": "assistant", "content": [{"type": "text", "text": "done"}]},
    ]


def test_anthropic_group_pairs_tool_use_with_result():
    msgs = _anthropic_conversation()
    assert message_group(msgs, 1, use_openai=False) == {1, 2}
    assert message_group(msgs, 2, use_openai=False) == {1, 2}
    assert message_group(msgs, 0, use_openai=False) == {0}


def test_anthropic_delete_removes_whole_pair():
    msgs = _anthropic_conversation()
    kept, deleted = delete_message_group(msgs, 1, use_openai=False)
    assert deleted == 2
    assert len(kept) == 2
    assert kept[0]["role"] == "user"
    assert kept[1]["role"] == "assistant"


# ─── describe ───────────────────────────────────────────────

def test_describe_messages_openai():
    rows = describe_messages(_openai_conversation(), use_openai=True)
    assert len(rows) == 6
    assert rows[0]["label"] == "(system prompt)"
    assert "read_file" in rows[2]["label"]
    assert rows[3]["chars"] == len("file contents here")


def test_describe_messages_anthropic():
    rows = describe_messages(_anthropic_conversation(), use_openai=False)
    assert "tool_use: read_file" in rows[1]["label"]
    assert "tool_result" in rows[2]["label"]


# ─── Agent integration ──────────────────────────────────────

def _make_agent():
    from agents.agent import Agent

    return Agent(model="deepseek-chat", api_base="https://example.com/v1", api_key="sk-test")


def test_agent_delete_context_messages():
    agent = _make_agent()
    agent._openai_messages = _openai_conversation()
    result = agent.delete_context_messages([2])
    assert "Deleted 2" in result
    assert agent._get_message_count() == 4


def test_agent_keep_context_messages():
    agent = _make_agent()
    agent._openai_messages = _openai_conversation()
    result = agent.keep_context_messages([5])
    assert "Kept 2" in result
    assert agent._get_message_count() == 2


def test_agent_describe_context():
    agent = _make_agent()
    agent._openai_messages = _openai_conversation()
    rows = agent.describe_context()
    assert len(rows) == 6
    assert rows[0]["role"] == "system"
