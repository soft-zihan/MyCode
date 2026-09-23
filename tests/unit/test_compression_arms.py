"""压缩消融实验臂（compression_arm）+ BC-8 每轮召回闩锁 单测。"""

from __future__ import annotations

import asyncio
import time

import pytest

from agents.agent import Agent
from agents.core.context_compressor import ContextCompressor
from agents.core.session import Session


def _make_agent(tmp_path, monkeypatch, **kwargs):
    monkeypatch.setenv("MYCODE_SESSION_DIR", str(tmp_path / "sessions"))
    defaults = dict(model="unknown-model", api_base="https://example.com/v1", api_key="sk-test")
    defaults.update(kwargs)
    return Agent(**defaults)


def _make_session(sid: str = "arm-test") -> Session:
    session = Session(sid, origin="sub_agent")
    session.append("user_message", {"content": "start"})
    return session


def _stub_folds(compressor: ContextCompressor, calls: dict) -> None:
    async def fake_tool(session, side_query):
        calls["tool"] += 1
        return True

    async def fake_session_fold(session, side_query, session_id):
        calls["session"] += 1
        return True

    compressor._fold_tool_results = fake_tool
    compressor._fold_session = fake_session_fold


async def test_arm_truncate_hides_oldest_no_summary():
    """truncate 臂：硬截断最旧消息组至目标水位，无摘要，保留首条任务指令。"""
    from agents.core.context_events import estimate_visible_message_tokens

    session = _make_session()
    for i in range(6):
        session.append("assistant_message", {
            "content": f"step {i} " + "x" * 200,
            "tool_calls": [{"id": f"c{i}", "function": {"name": "bash", "arguments": "{}"}}],
        })
        session.append("tool_result_msg", {"call_id": f"c{i}", "content": f"result {i} " + "y" * 400})

    compressor = ContextCompressor(effective_window=1000, tool_fold_threshold=0.5, arm="truncate")
    current = estimate_visible_message_tokens(session)
    assert current > 500, "测试前提：利用率须超过触发线"

    result = await compressor.run_pipeline(session, current, time.time(), None, "s")
    assert result is True

    types = [e.get("type") for e in session.events]
    assert "events_hidden" in types
    assert "tool_folded" not in types and "session_folded" not in types, "截断臂不得产生摘要"

    visible = session.visible_events
    assert any(e.get("type") == "user_message" for e in visible), "首条任务指令必须保留"
    assert estimate_visible_message_tokens(session) <= 1000 * 0.40
    assert compressor.get_stats()["truncate"]["triggered"] == 1
    # 派生消息不出现孤儿 tool 结果（tool_call/tool_result 整组隐藏）
    msgs = session.get_messages_for_llm()
    tool_ids_with_call = {
        tc["id"] for m in msgs if m["role"] == "assistant" for tc in (m.get("tool_calls") or [])
    }
    assert all(m["tool_call_id"] in tool_ids_with_call for m in msgs if m["role"] == "tool")


async def test_arm_truncate_ignores_idle_and_low_utilization():
    compressor = ContextCompressor(
        effective_window=10000, tool_fold_threshold=0.5, idle_timeout_seconds=1, arm="truncate",
    )
    session = _make_session()
    result = await compressor.run_pipeline(session, 10, time.time() - 1000, None, "s")
    assert result is False


async def test_arm_tool_only_skips_session_fold():
    compressor = ContextCompressor(effective_window=100, tool_fold_threshold=0.5, arm="tool_only")
    calls = {"tool": 0, "session": 0}
    _stub_folds(compressor, calls)
    result = await compressor.run_pipeline(_make_session(), 90, time.time(), None, "s")
    assert result is True
    assert calls == {"tool": 1, "session": 0}


async def test_arm_session_only_skips_tool_fold():
    compressor = ContextCompressor(effective_window=100, tool_fold_threshold=0.5, arm="session_only")
    calls = {"tool": 0, "session": 0}
    _stub_folds(compressor, calls)
    result = await compressor.run_pipeline(_make_session(), 90, time.time(), None, "s")
    assert result is True
    assert calls == {"tool": 0, "session": 1}


async def test_experimental_arm_ignores_idle_trigger():
    """实验臂只保留利用率触发：低利用率 + 超长 idle 不压缩（避免 idle 折叠混入另一机制）。"""
    compressor = ContextCompressor(
        effective_window=10000, tool_fold_threshold=0.5, idle_timeout_seconds=1, arm="tool_only",
    )
    calls = {"tool": 0, "session": 0}
    _stub_folds(compressor, calls)
    result = await compressor.run_pipeline(_make_session(), 10, time.time() - 1000, None, "s")
    assert result is False
    assert calls == {"tool": 0, "session": 0}


async def test_full_arm_keeps_idle_trigger():
    compressor = ContextCompressor(
        effective_window=10000, tool_fold_threshold=0.5, idle_timeout_seconds=1, arm="full",
    )
    calls = {"tool": 0, "session": 0}
    _stub_folds(compressor, calls)
    await compressor.run_pipeline(_make_session(), 10, time.time() - 1000, None, "s")
    assert calls["tool"] == 1


def test_invalid_arm_raises():
    with pytest.raises(ValueError):
        ContextCompressor(effective_window=100, tool_fold_threshold=0.5, arm="bogus")


def test_agent_truncate_arm_keeps_window(tmp_path, monkeypatch):
    agent = _make_agent(tmp_path, monkeypatch, compression_arm="truncate", context_window=100_000)
    assert agent.context_window == 100_000
    assert agent.effective_window == 80_000
    assert agent._compressor.arm == "truncate"


def test_agent_thinking_feedback_default_false(tmp_path, monkeypatch):
    agent = _make_agent(tmp_path, monkeypatch)
    assert agent.thinking_feedback is False
    assert agent.session.thinking_feedback is False


def test_thinking_feedback_controls_reasoning_content():
    off = _make_session("tf-off")
    off.append("assistant_message", {"content": "answer", "thinking": "deep thought"})
    asst = [m for m in off.get_messages_for_llm() if m["role"] == "assistant"][-1]
    assert "thinking" not in asst and "reasoning_content" not in asst

    on = _make_session("tf-on")
    on.thinking_feedback = True
    on.append("assistant_message", {"content": "answer", "thinking": "deep thought"})
    asst2 = [m for m in on.get_messages_for_llm() if m["role"] == "assistant"][-1]
    assert asst2["reasoning_content"] == "deep thought"


def test_agent_default_arm_is_full(tmp_path, monkeypatch):
    agent = _make_agent(tmp_path, monkeypatch)
    assert agent.compression_arm == "full"
    assert agent._compressor.arm == "full"


def test_agent_invalid_arm_raises(tmp_path, monkeypatch):
    with pytest.raises(ValueError):
        _make_agent(tmp_path, monkeypatch, compression_arm="bogus")


async def test_wiki_prefetch_rearms_after_consume(tmp_path, monkeypatch):
    """BC-8：prefetch 消费后下一轮必须能重新召回（此前引用永不清空，session 只召回一次）。"""
    agent = _make_agent(tmp_path, monkeypatch)
    queries: list[str] = []

    async def fake_select(query, side_query, cooled):
        queries.append(query)
        return []

    monkeypatch.setattr("agents.wiki.wiki_manager.select_relevant_wiki_entries", fake_select)

    agent.start_wiki_prefetch("m1", object())
    first = agent._wiki_prefetch
    assert first is not None

    # 在途未消费：不重复起
    agent.start_wiki_prefetch("m1-again", object())
    assert agent._wiki_prefetch is first

    await first
    # 完成但未消费：仍不起新的（等 consume）
    agent.start_wiki_prefetch("m1-again", object())
    assert agent._wiki_prefetch is first
    assert queries == ["m1"]

    # 消费后：下一轮重新召回
    agent._wiki_prefetch_consumed = True
    agent.start_wiki_prefetch("m2", object())
    assert agent._wiki_prefetch is not first
    await agent._wiki_prefetch
    assert queries == ["m1", "m2"]
