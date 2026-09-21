"""Phase 2：segment 捕获清洗 + 提取字段校验单测。"""

from __future__ import annotations

import json

import pytest

from agents.core.frontmatter import parse_frontmatter
from agents.core.workspace import workspace_scope
from agents.wiki.wiki_capture import capture_session_to_session
from agents.wiki.wiki_compiler import _validate_extraction


@pytest.fixture
def ws(tmp_path, monkeypatch):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path))
    with workspace_scope(workspace):
        yield workspace


def _capture(events: list[dict], ws):
    path = capture_session_to_session("sess-x", events, 1)
    assert path is not None
    return parse_frontmatter(path.read_text())


def test_tool_result_truncated_to_500(ws):
    events = [
        {"seq": 1, "type": "tool_result_msg", "tool_name": "bash", "call_id": "c1",
         "content": "x" * 3000},
    ]
    result = _capture(events, ws)
    body = result.body
    assert "[truncated]" in body
    # 500 字符 + 截断标记，远小于原文
    assert "x" * 501 not in body


def test_external_tool_replaced_with_placeholder(ws):
    events = [
        {"seq": 1, "type": "tool_result_msg", "tool_name": "webfetch", "call_id": "c1",
         "content": "第三方网页正文" * 50},
        {"seq": 2, "type": "tool_result_msg", "tool_name": "mcp__playwright__browser_click", "call_id": "c2",
         "content": "page snapshot"},
    ]
    body = _capture(events, ws).body
    assert "[external content from webfetch omitted]" in body
    assert "第三方网页正文" not in body
    assert "[external content from mcp__playwright__browser_click omitted]" in body


def test_secrets_redacted_in_segment(ws):
    events = [
        {"seq": 1, "type": "user_message", "content": "用 sk-livekey123456789 部署"},
    ]
    body = _capture(events, ws).body
    assert "sk-livekey123456789" not in body
    assert "[REDACTED:openai_key]" in body


def test_context_budget_head_tail_truncation(ws, monkeypatch):
    # 把 side 上下文压到很小，强制触发总预算截断
    import agents.wiki.wiki_capture as wc
    monkeypatch.setattr(wc, "_capture_settings", lambda: {
        "tool_result_max_chars": 100000,
        "context_ratio": 0.7,
        "side_context_window": 1000,
        "external_tools": [],
    })
    events = [
        {"seq": i, "type": "user_message", "content": f"msg{i} " + "y" * 50}
        for i in range(30)
    ]
    result = _capture(events, ws)
    assert result.meta["truncated"] == "true"
    assert "[middle truncated:" in result.body
    # 头尾保留、中部丢弃
    assert "msg0 " in result.body
    assert "msg29 " in result.body
    assert "msg15 " not in result.body
    assert len(result.body) < 1000


def test_no_truncation_flag_when_within_budget(ws):
    events = [{"seq": 1, "type": "user_message", "content": "hello"}]
    assert _capture(events, ws).meta["truncated"] == "false"


# ─── 提取字段校验 ───

def test_validate_extraction_rules():
    ok = {"type": "knowledge", "name": "n", "description": "d", "content": "c"}
    assert _validate_extraction(ok) is None

    assert _validate_extraction({"type": "plan", "name": "n", "description": "d", "content": "c"})
    assert _validate_extraction({"type": "knowledge", "name": "", "description": "d", "content": "c"})
    assert _validate_extraction({"type": "knowledge", "name": "n", "description": "", "content": "c"})
    assert _validate_extraction({"type": "knowledge", "name": "n", "description": "d", "content": ""})

    pattern_missing = {"type": "workflow_pattern", "name": "n", "description": "d", "symptom": "s"}
    reason = _validate_extraction(pattern_missing)
    assert reason and "workaround" in reason

    pattern_ok = {"type": "workflow_pattern", "name": "n", "description": "d",
                  "symptom": "s", "root_cause": "r", "workaround": "w"}
    assert _validate_extraction(pattern_ok) is None

    feedback_ok = {"type": "feedback", "name": "n", "description": "d", "content": "rule/why/how"}
    assert _validate_extraction(feedback_ok) is None
