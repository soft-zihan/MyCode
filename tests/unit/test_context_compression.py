import asyncio
import json

import pytest

from agents.agent import Agent
from agents.core.context_compressor import (
    REQUIRED_NOTES_SECTIONS,
    ContextCompressor,
    validate_session_notes,
)
from agents.core.session import Session


def _valid_notes(marker: str = "") -> str:
    """结构合格的 session_notes（含全部必需 ## 小节）。"""
    body = "\n\n".join(f"{s}\n- item{marker}" for s in REQUIRED_NOTES_SECTIONS)
    return body


def _make_agent(tmp_path, monkeypatch, **kwargs):
    monkeypatch.setenv("MYCODE_SESSION_DIR", str(tmp_path / "sessions"))
    defaults = dict(model="unknown-model", api_base="https://example.com/v1", api_key="sk-test")
    defaults.update(kwargs)
    return Agent(**defaults)


def _assert_valid_tool_pairs(messages):
    open_call_ids: set[str] = set()
    for message in messages:
        role = message.get("role")
        if role == "assistant" and message.get("tool_calls"):
            open_call_ids = {
                str(tc.get("id"))
                for tc in message["tool_calls"]
                if isinstance(tc, dict) and tc.get("id")
            }
        elif role == "tool":
            call_id = str(message.get("tool_call_id") or "")
            assert call_id in open_call_ids
            open_call_ids.discard(call_id)
        else:
            open_call_ids.clear()
    assert not open_call_ids


def _append_tool_round(session, index: int, *, content: str | None = None, assistant_text: str | None = None):
    call_id = f"call_{index}"
    session.append("assistant_message", {
        "content": assistant_text,
        "tool_calls": [{
            "id": call_id,
            "type": "function",
            "function": {
                "name": "read_file",
                "arguments": json.dumps({"path": f"file_{index}.py"}),
            },
        }],
    })
    session.append("tool_result_msg", {
        "call_id": call_id,
        "content": f"result {index}" if content is None else content,
    })
    return call_id


async def test_tool_fold_preserves_tool_call_pairs():
    session = Session("tool-fold-pairs", origin="sub_agent")
    session.append("user_message", {"content": "start"})
    for index in range(5):
        _append_tool_round(session, index)

    compressor = ContextCompressor(
        effective_window=1000,
        tool_fold_threshold=0.7,
        keep_recent_tool_rounds=2,
    )
    assert await compressor._fold_tool_results(session, None)

    messages = session.get_messages_for_llm()
    _assert_valid_tool_pairs(messages)

    folded_events = [event for event in session.visible_events if event.get("type") == "tool_folded"]
    assert len(folded_events) == 1
    folded_call_ids = {abstract["call_id"] for abstract in folded_events[0]["abstracts"]}
    assert {"call_0", "call_1", "call_2"} <= folded_call_ids
    assert "call_3" not in folded_call_ids


async def test_tool_fold_merges_previous_folded_results():
    session = Session("tool-fold-merge", origin="sub_agent")
    session.append("user_message", {"content": "start"})
    for index in range(5):
        _append_tool_round(session, index)

    compressor = ContextCompressor(
        effective_window=1000,
        tool_fold_threshold=0.7,
        keep_recent_tool_rounds=2,
    )
    assert await compressor._fold_tool_results(session, None)

    for index in range(5, 8):
        _append_tool_round(session, index)
    assert await compressor._fold_tool_results(session, None)

    folded_events = [event for event in session.visible_events if event.get("type") == "tool_folded"]
    assert len(folded_events) == 1
    folded_call_ids = {abstract["call_id"] for abstract in folded_events[0]["abstracts"]}
    assert {"call_0", "call_1", "call_2", "call_3", "call_4", "call_5"} <= folded_call_ids
    assert {"call_6", "call_7"}.isdisjoint(folded_call_ids)
    _assert_valid_tool_pairs(session.get_messages_for_llm())


async def test_tool_fold_preserves_assistant_text_and_compresses_long_results():
    session = Session("tool-fold-compress", origin="sub_agent")
    session.append("user_message", {"content": "start"})
    long_content = "x" * 5000
    _append_tool_round(session, 0, content=long_content, assistant_text="checking file_0.py")
    for index in range(1, 4):
        _append_tool_round(session, index)

    compressor = ContextCompressor(
        effective_window=1000,
        tool_fold_threshold=0.7,
        keep_recent_tool_rounds=2,
        tool_abstract_char_limit=500,
    )
    assert await compressor._fold_tool_results(session, None)

    folded_events = [event for event in session.visible_events if event.get("type") == "tool_folded"]
    assert len(folded_events) == 1
    folded_event = folded_events[0]
    abstract = next(item for item in folded_event["abstracts"] if item["call_id"] == "call_0")
    assert len(abstract["abstract"]) < len(long_content)
    assert "clipped" in abstract["abstract"]
    assert folded_event["assistant_texts"][0]["text"] == "checking file_0.py"

    rendered = "\n".join(str(message.get("content") or "") for message in session.get_messages_for_llm())
    assert "checking file_0.py" in rendered
    assert long_content not in rendered


async def test_tool_fold_uses_batch_side_query_for_long_results():
    session = Session("tool-fold-side-query", origin="sub_agent")
    session.append("user_message", {"content": "start"})
    for index in range(4):
        _append_tool_round(session, index, content="y" * 3000)

    prompts: list[tuple[str, str]] = []

    async def side_query(system: str, user: str) -> str:
        prompts.append((system, user))
        return json.dumps({
            "abstracts": [
                {"call_id": "call_0", "abstract": "summary 0"},
                {"call_id": "call_1", "abstract": "summary 1"},
            ]
        })

    compressor = ContextCompressor(
        effective_window=1000,
        tool_fold_threshold=0.7,
        keep_recent_tool_rounds=2,
        tool_abstract_char_limit=500,
    )
    assert await compressor._fold_tool_results(session, side_query)

    assert len(prompts) == 1
    folded_events = [event for event in session.visible_events if event.get("type") == "tool_folded"]
    abstracts = {item["call_id"]: item["abstract"] for item in folded_events[0]["abstracts"]}
    assert abstracts["call_0"] == "summary 0"
    assert abstracts["call_1"] == "summary 1"
    assert "call_2" not in abstracts


async def test_tool_fold_bounds_batch_side_query_input():
    session = Session("tool-fold-batch-budget", origin="sub_agent")
    session.append("user_message", {"content": "start"})
    _append_tool_round(session, 0, content="a" * 6000)
    _append_tool_round(session, 1, content="b" * 1800)
    _append_tool_round(session, 2, content="c" * 1800)

    prompts: list[tuple[str, str]] = []

    async def side_query(system: str, user: str) -> str:
        prompts.append((system, user))
        return json.dumps({"abstracts": [{"call_id": "call_0", "abstract": "summary 0"}]})

    compressor = ContextCompressor(
        effective_window=1000,
        tool_fold_threshold=0.7,
        keep_recent_tool_rounds=1,
        tool_abstract_char_limit=500,
        tool_abstract_input_char_limit=2000,
        tool_abstract_batch_char_limit=2500,
    )
    assert await compressor._fold_tool_results(session, side_query)

    assert len(prompts) == 1
    payload = json.loads(prompts[0][1].split("\n", 1)[1])
    assert [item["call_id"] for item in payload] == ["call_0", "call_1"]
    assert 1500 < len(payload[0]["content"]) <= 2000
    assert len(payload[1]["content"]) < 1800
    assert "batch input truncated" in payload[1]["content"]

    folded_events = [event for event in session.visible_events if event.get("type") == "tool_folded"]
    abstracts = {item["call_id"]: item["abstract"] for item in folded_events[0]["abstracts"]}
    assert abstracts["call_0"] == "summary 0"
    assert "clipped" in abstracts["call_1"]


async def test_session_fold_merges_previous_notes_and_replaces_old_summary(monkeypatch):
    session = Session("session-fold", origin="sub_agent")
    old_fold = session.append("session_folded", {
        "summary": "old summary",
        "session_notes": "old notes",
        "project_knowledge": "old knowledge",
        "trigger": "auto",
    })
    for index in range(4):
        session.append("user_message", {"content": f"user {index}"})
        session.append("assistant_message", {"content": f"assistant {index}"})

    prompts: list[tuple[str, str]] = []

    async def side_query(system: str, user: str) -> str:
        prompts.append((system, user))
        return json.dumps({
            "session_notes": _valid_notes(" merged"),
            "project_knowledge": "merged knowledge",
        })

    wiki_calls: list[dict] = []

    async def fake_on_session_folded(**kwargs):
        wiki_calls.append(kwargs)

    import agents.wiki.pipeline as wiki_pipeline
    monkeypatch.setattr(wiki_pipeline, "on_session_folded", fake_on_session_folded)

    compressor = ContextCompressor(
        effective_window=1000,
        tool_fold_threshold=0.7,
        keep_recent_dialog_rounds=2,
    )
    assert await compressor._fold_session(session, side_query, "sess")
    await asyncio.sleep(0)

    visible_folds = [event for event in session.visible_events if event.get("type") == "session_folded"]
    assert len(visible_folds) == 1
    assert visible_folds[0]["session_notes"] == _valid_notes(" merged")
    assert visible_folds[0]["notes_validated"] is True
    assert old_fold["seq"] not in session.visible_seqs

    assert len(prompts) == 1
    compile_prompt = prompts[0][1]
    assert "old notes" in compile_prompt
    assert "user 0" in compile_prompt
    assert "user 3" not in compile_prompt

    assert len(wiki_calls) == 1
    assert wiki_calls[0]["session_id"] == "sess"
    assert wiki_calls[0]["session_notes"] == _valid_notes(" merged")

    messages = session.get_messages_for_llm()
    contents = [str(message.get("content") or "") for message in messages]
    assert not any("old summary" in content for content in contents)
    assert any("item merged" in content for content in contents)


def test_agent_uses_default_auto_compact_threshold(tmp_path, monkeypatch):
    agent = _make_agent(tmp_path, monkeypatch)
    assert agent._compressor.effective_window == agent.effective_window
    assert agent._compressor.tool_fold_threshold == agent.auto_compact_threshold


def test_estimated_context_tokens_include_events_after_last_usage(tmp_path, monkeypatch):
    agent = _make_agent(tmp_path, monkeypatch)
    agent.set_last_usage_tokens(100, 150)
    assistant_event = agent.session.append("assistant_message", {"content": "hello"})
    agent.mark_last_usage_position(int(assistant_event["seq"]))

    assert agent.last_total_token_count == 150
    assert agent.estimated_context_tokens == 150

    agent.session.append("tool_result_msg", {"call_id": "call_1", "content": "x" * 400})
    assert agent.estimated_context_tokens > 150

    agent.reset_context_token_estimate()
    assert agent.estimated_context_tokens == 0


async def test_session_fold_single_turn_trajectory_keeps_turn_running():
    session = Session("trajectory-fold", origin="sub_agent")
    session.append("user_message", {"content": "long running task"})
    for index in range(7):
        _append_tool_round(session, index)

    compressor = ContextCompressor(
        effective_window=1000,
        tool_fold_threshold=0.7,
        keep_recent_trajectory_tool_rounds=5,
    )
    assert await compressor._fold_session(session, None, "trajectory-fold")

    visible_types = [event.get("type") for event in session.visible_events]
    assert "session_folded" in visible_types
    assert "turn/end" not in [event.get("type") for event in session.events]

    messages = session.get_messages_for_llm()
    assert messages[0]["role"] == "user"
    assert "long running task" in str(messages[0]["content"])
    _assert_valid_tool_pairs(messages)

    visible_call_ids = {
        event.get("call_id")
        for event in session.visible_events
        if event.get("type") == "tool_result_msg"
    }
    assert {"call_2", "call_3", "call_4", "call_5", "call_6"} <= visible_call_ids
    assert not {"call_0", "call_1"} & visible_call_ids

    folded_event = next(event for event in session.visible_events if event.get("type") == "session_folded")
    assert folded_event["fold_mode"] == "trajectory"


def test_session_notes_wiki_entry_is_updated_in_place(tmp_path):
    from agents.core.workspace import reset_workspace, set_workspace
    from agents.wiki.store import get_wiki_dir, write_wiki_entry

    token = set_workspace(tmp_path)
    try:
        first = write_wiki_entry(
            "session_notes",
            "session_abc",
            "note v1",
            description="Session notes",
            extra_meta={"session_id": "abc"},
            skip_if_unchanged=True,
        )
        unchanged = write_wiki_entry(
            "session_notes",
            "session_abc",
            "note v1",
            description="Session notes",
            extra_meta={"session_id": "abc"},
            skip_if_unchanged=True,
        )
        updated = write_wiki_entry(
            "session_notes",
            "session_abc",
            "note v2",
            description="Session notes",
            extra_meta={"session_id": "abc"},
            skip_if_unchanged=True,
        )

        assert first == unchanged == updated
        note_files = list((get_wiki_dir() / "session_notes").glob("*.md"))
        assert len(note_files) == 1
        assert "note v2" in updated.read_text()
    finally:
        reset_workspace(token)


# ── U5a：session_notes 结构校验 + 纠正重试 ──────────────────────


def test_validate_session_notes_detects_missing_sections():
    assert validate_session_notes(_valid_notes()) == []
    missing = validate_session_notes("## Objective\n- x\n## Next Move\n1. y")
    assert "## Requirements" in missing
    assert "## Objective" not in missing


def _foldable_session() -> Session:
    session = Session("notes-validation", origin="sub_agent")
    for index in range(4):
        session.append("user_message", {"content": f"user {index}"})
        session.append("assistant_message", {"content": f"assistant {index}"})
    return session


def _make_fold_compressor() -> ContextCompressor:
    return ContextCompressor(
        effective_window=1000,
        tool_fold_threshold=0.7,
        keep_recent_dialog_rounds=2,
    )


async def test_notes_invalid_then_retry_valid(monkeypatch):
    session = _foldable_session()
    prompts: list[str] = []
    calls = {"n": 0}

    async def side_query(system: str, user: str) -> str:
        prompts.append(user)
        calls["n"] += 1
        if calls["n"] == 1:
            return json.dumps({"session_notes": "## Objective\n- broken", "project_knowledge": ""})
        return json.dumps({"session_notes": _valid_notes(" retry"), "project_knowledge": "k"})

    import agents.wiki.pipeline as wiki_pipeline
    async def _noop(**kwargs):
        return None
    monkeypatch.setattr(wiki_pipeline, "on_session_folded", _noop)

    compressor = _make_fold_compressor()
    assert await compressor._fold_session(session, side_query, "sess")
    await asyncio.sleep(0)

    assert calls["n"] == 2
    assert "missing required sections" in prompts[1]
    fold = [e for e in session.visible_events if e.get("type") == "session_folded"][0]
    assert fold["notes_validated"] is True
    assert fold["session_notes"] == _valid_notes(" retry")


async def test_notes_invalid_twice_keeps_content_and_audits(monkeypatch):
    session = _foldable_session()

    async def side_query(system: str, user: str) -> str:
        return json.dumps({"session_notes": "## Objective\n- still broken", "project_knowledge": ""})

    import agents.wiki.pipeline as wiki_pipeline
    async def _noop(**kwargs):
        return None
    monkeypatch.setattr(wiki_pipeline, "on_session_folded", _noop)

    compressor = _make_fold_compressor()
    assert await compressor._fold_session(session, side_query, "sess")
    await asyncio.sleep(0)

    fold = [e for e in session.visible_events if e.get("type") == "session_folded"][0]
    # 内容优先于格式：结构不合格仍保留 notes，但审计字段显式标记失败
    assert fold["notes_validated"] is False
    assert "still broken" in fold["session_notes"]
    assert "still broken" in fold["summary"]


async def test_notes_side_query_exception_falls_back(monkeypatch):
    session = _foldable_session()

    async def side_query(system: str, user: str) -> str:
        raise RuntimeError("gateway down")

    compressor = _make_fold_compressor()
    assert await compressor._fold_session(session, side_query, "sess")
    await asyncio.sleep(0)

    fold = [e for e in session.visible_events if e.get("type") == "session_folded"][0]
    assert fold["session_notes"] == ""
    assert fold["notes_validated"] is False
    assert fold["summary"]  # 确定性 fallback 摘要非空


class TestCheckpointTail:
    """U5b-#3 v2 conversation-checkpoint 双层结构：摘要 + 近期无损尾部按 token 预算扩展。"""

    @staticmethod
    def _dialog_groups(pairs: int, chars: int):
        from agents.core.context_events import EventGroup

        groups = []
        seq = 0
        for i in range(pairs):
            groups.append(EventGroup("user", [{
                "type": "user_message", "seq": seq, "content": f"u{i} " + "x" * chars,
            }]))
            seq += 1
            groups.append(EventGroup("assistant_text", [{
                "type": "assistant_message", "seq": seq, "content": f"a{i} " + "y" * chars,
            }]))
            seq += 1
        return groups

    def test_budget_zero_keeps_round_based_cut(self):
        from agents.core.context_events import find_session_fold_cut

        groups = self._dialog_groups(8, 12_000)  # 每组 ~3000 tokens
        assert find_session_fold_cut(groups, 2, 5) == 12

    def test_tail_extends_to_token_budget(self):
        from agents.core.context_events import find_session_fold_cut

        groups = self._dialog_groups(8, 12_000)
        # rounds cut=12 → 尾部 4 组=12k tokens < 15k 预算 → 前扩 1 组到 15k
        assert find_session_fold_cut(groups, 2, 5, tail_token_budget=15_000) == 11

    def test_small_content_not_extended(self):
        from agents.core.context_events import find_session_fold_cut

        groups = self._dialog_groups(8, 40)  # 总量 ~160 tokens，可折叠部分 < min_fold 2000
        assert find_session_fold_cut(groups, 2, 5, tail_token_budget=15_000) == 12

    def test_all_user_degenerate_guarded(self):
        from agents.core.context_events import EventGroup, find_session_fold_cut

        groups = [
            EventGroup("user", [{"type": "user_message", "seq": i, "content": "x" * 12_000}])
            for i in range(6)
        ]
        # 扩展把 cut 压到 1，groups[:1] 全 user → 拒绝折叠
        assert find_session_fold_cut(groups, 2, 5, tail_token_budget=15_000) == -1
