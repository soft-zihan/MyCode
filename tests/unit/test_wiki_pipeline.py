"""wiki.pipeline 端到端单测：折叠事件 → session_notes + segment 捕获 → 编译 → 水位线。"""

from __future__ import annotations

import json

import pytest

from agents.core.frontmatter import parse_frontmatter
from agents.core.workspace import workspace_scope


class FakeSession:
    def __init__(self, events: list[dict]):
        self._events = events

    @property
    def events(self) -> tuple[dict, ...]:
        return tuple(self._events)


def _fold_events(session_id: str = "s1") -> list[dict]:
    return [
        {"seq": 1, "type": "user_message", "content": "pytest 挂了怎么办"},
        {
            "seq": 2,
            "type": "assistant_message",
            "content": "",
            "tool_calls": [
                {"id": "c1", "function": {"name": "bash", "arguments": '{"command": "pytest"}'}}
            ],
        },
        {
            "seq": 3,
            "type": "tool_result_msg",
            "call_id": "c1",
            "content": '<tool_result tool="bash">\n1 failed\n</tool_result>',
        },
    ]


@pytest.fixture
def ws(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    with workspace_scope(workspace):
        yield workspace


async def test_fold_writes_notes_captures_compiles_and_advances_watermark(ws):
    from agents.wiki.pipeline import on_session_folded
    from agents.wiki.wiki_capture import get_last_extract_pos
    from agents.wiki.wiki_manager import get_wiki_dir

    async def side_query(system: str, user: str) -> str:
        return json.dumps([{
            "type": "knowledge",
            "name": "pytest 修复方法",
            "description": "调试经验",
            "content": "先跑 pytest -x 定位",
        }], ensure_ascii=False)

    await on_session_folded(
        session_id="s1",
        session=FakeSession(_fold_events()),
        session_notes="用户在修 pytest",
        side_query=side_query,
    )

    wiki_dir = get_wiki_dir()

    # session_notes 写入
    notes_path = wiki_dir / "session_notes" / "session_s1.md"
    assert notes_path.exists()
    assert "用户在修 pytest" in notes_path.read_text()

    # segment 捕获且编译标记推进
    segments = list((wiki_dir / "session").rglob("s1_seg*.md"))
    assert len(segments) == 1
    seg_meta = parse_frontmatter(segments[0].read_text()).meta
    assert seg_meta["compiled"] == "true"
    assert seg_meta["max_seq"] == "3"

    # B1：tool_result 带真实工具名而非 unknown
    seg_body = parse_frontmatter(segments[0].read_text()).body
    assert "## Tool Result: bash" in seg_body
    assert "Tool Result: unknown" not in seg_body

    # 提取条目入库
    entries = list((wiki_dir / "knowledge").rglob("*.md"))
    assert entries, "knowledge entry not written"
    assert "pytest" in entries[0].read_text()

    # 水位线推进到 max_seq
    assert get_last_extract_pos("s1") == 3


async def test_compile_failure_keeps_watermark_and_segment_uncompiled(ws):
    from agents.wiki.pipeline import on_session_folded
    from agents.wiki.wiki_capture import get_last_extract_pos
    from agents.wiki.wiki_manager import get_wiki_dir

    async def broken_side_query(system: str, user: str) -> str:
        raise RuntimeError("llm down")

    await on_session_folded(
        session_id="s2",
        session=FakeSession(_fold_events()),
        session_notes="",
        side_query=broken_side_query,
    )

    wiki_dir = get_wiki_dir()
    segments = list((wiki_dir / "session").rglob("s2_seg*.md"))
    assert len(segments) == 1
    meta = parse_frontmatter(segments[0].read_text()).meta
    assert meta["compiled"] == "false"
    assert get_last_extract_pos("s2") == 0


async def test_watermark_prevents_recapture(ws):
    from agents.wiki.pipeline import on_session_folded
    from agents.wiki.wiki_manager import get_wiki_dir

    async def side_query(system: str, user: str) -> str:
        return "[]"

    session = FakeSession(_fold_events())
    await on_session_folded(session_id="s3", session=session, session_notes="", side_query=side_query)
    await on_session_folded(session_id="s3", session=session, session_notes="", side_query=side_query)

    segments = list((get_wiki_dir() / "session").rglob("s3_seg*.md"))
    assert len(segments) == 1
