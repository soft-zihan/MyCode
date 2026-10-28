"""悬挂 tool_call 的派生期补齐 + 工具重放策略。

真实流程是 assistant_message（带 tool_calls）先落盘、工具后执行，所以硬杀
（kill -9 / OOM / Pod 驱逐）会在事件流里留下永远等不到 tool_result_msg 的调用。
OpenAI 兼容接口对「assistant.tool_calls 后面没有 role=tool」的形状直接 400，
这个会话从此续不下去。这组测试钉住补齐后的形状与措辞。
"""

from __future__ import annotations

import pytest

from agents.core.dangling_tool_calls import (
    REPLAY_SAFE,
    REPLAY_UNSAFE,
    ensure_call_outputs_present,
    replay_policy,
    unterminated_sub_sessions,
)


def _assistant(*calls: tuple[str, str]) -> dict:
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {"id": cid, "type": "function", "function": {"name": name, "arguments": "{}"}}
            for cid, name in calls
        ],
    }


def _tool(call_id: str, content: str = "ok") -> dict:
    return {"role": "tool", "tool_call_id": call_id, "content": content}


def _answered_ids(messages: list[dict]) -> set[str]:
    return {m["tool_call_id"] for m in messages if m.get("role") == "tool"}


# ── 形状：每个 tool_call 都必须有结果 ──────────────────────────────

def test_dangling_call_gets_a_result():
    out = ensure_call_outputs_present([_assistant(("c1", "run_shell"))])
    assert _answered_ids(out) == {"c1"}


def test_answered_call_is_left_alone():
    msgs = [_assistant(("c1", "run_shell")), _tool("c1", "done")]
    out = ensure_call_outputs_present(msgs)
    assert len(out) == 2, "已经有结果的调用不该被再补一条"
    assert out[1]["content"] == "done"


def test_answer_appearing_later_in_history_still_counts():
    """结果不一定紧跟在 assistant 之后（中间可能夹别的事件投影出的消息），
    只要历史里存在就算已答，否则会出现两条同 id 的 tool 消息——那同样是 400。"""
    msgs = [
        _assistant(("c1", "run_shell")),
        {"role": "user", "content": "继续"},
        _tool("c1", "迟到的结果"),
    ]
    out = ensure_call_outputs_present(msgs)
    assert len(out) == 3
    assert [m["tool_call_id"] for m in out if m.get("role") == "tool"] == ["c1"]


def test_two_dangling_calls_answered_in_call_order():
    out = ensure_call_outputs_present([_assistant(("c1", "run_shell"), ("c2", "read_file"))])
    tools = [m for m in out if m.get("role") == "tool"]
    assert [m["tool_call_id"] for m in tools] == ["c1", "c2"], "顺序错了网关同样会拒"


def test_messages_without_tool_calls_are_untouched():
    msgs = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}]
    assert ensure_call_outputs_present(msgs) == msgs


def test_input_list_is_not_mutated():
    msgs = [_assistant(("c1", "run_shell"))]
    ensure_call_outputs_present(msgs)
    assert len(msgs) == 1, "补齐只能作用在返回值上，不能改调用方的列表（那是派生缓存）"


# ── 措辞：重放策略决定模型下一步该干什么 ───────────────────────────

def test_unsafe_tool_says_it_may_have_partially_run():
    content = ensure_call_outputs_present([_assistant(("c1", "run_shell"))])[1]["content"]
    assert "可能已" in content and "run_shell" in content
    assert "核实" in content, "要引导模型先核实现场，而不是直接重跑"


def test_safe_tool_says_retry_is_fine():
    content = ensure_call_outputs_present([_assistant(("c1", "write_file"))])[1]["content"]
    assert "可安全重跑" in content
    assert "可能已" not in content, "同参数收敛的操作不该吓模型去反复核实"


def test_unknown_and_third_party_tools_default_to_unsafe():
    """MCP 工具与任何没登记过的名字都必须落到 unsafe——默认放行重放等于
    把「重复扣款 / 重复建 PR」这类副作用交给运气。"""
    assert replay_policy("mcp__github__create_pr") == REPLAY_UNSAFE
    assert replay_policy("从来没见过的工具") == REPLAY_UNSAFE
    assert replay_policy("run_shell") == REPLAY_UNSAFE
    assert replay_policy("edit_file") == REPLAY_UNSAFE, "old_string 匹配用过一次就不在了，重跑不收敛"
    assert replay_policy("agent") == REPLAY_UNSAFE, "重跑会再派一个子 Agent"
    assert replay_policy("write_file") == REPLAY_SAFE
    assert replay_policy("read_file") == REPLAY_SAFE


def test_every_read_only_tool_except_compact_is_declared_safe():
    """只读工具天然是 safe 的；漏登记会让模型在恢复后对一次 grep 也去反复核实。
    compact_context 是唯一的例外：它归在 READ_TOOLS 是因为「不需要用户批准」，
    但它会改会话（再压一次就再少一段），同参数重跑不收敛。"""
    from agents.tools.registry import READ_TOOLS

    for name in sorted(READ_TOOLS - {"compact_context"}):
        assert replay_policy(name) == REPLAY_SAFE, f"{name} 是只读工具，应声明为 safe"
    assert replay_policy("compact_context") == REPLAY_UNSAFE


# ── 子 Agent：中断结果里要带上还能续跑的会话 id ────────────────────

def test_unterminated_sub_sessions_come_from_the_event_stream():
    events = [
        {"type": "sub_agent/start", "sub_session_id": "sub-1"},
        {"type": "sub_agent/start", "sub_session_id": "sub-2"},
        {"type": "sub_agent/end", "sub_session_id": "sub-2"},
    ]
    assert unterminated_sub_sessions(events) == ["sub-1"]


def test_interrupted_subagent_call_names_the_sub_session():
    """这是硬杀之后模型能不能自己续跑的关键：子会话 id 已经落在 sub_agent/start 里，
    只是从来没被投影进 LLM 消息，所以模型看不见它。"""
    events = [{"type": "sub_agent/start", "sub_session_id": "sub-abc"}]
    out = ensure_call_outputs_present([_assistant(("c1", "agent"))], events=events)
    content = out[1]["content"]
    assert "sub-abc" in content
    assert "session_id" in content, "要告诉模型用哪个参数续跑"


def test_terminated_sub_session_is_not_offered_for_resume():
    events = [
        {"type": "sub_agent/start", "sub_session_id": "sub-done"},
        {"type": "sub_agent/end", "sub_session_id": "sub-done"},
    ]
    content = ensure_call_outputs_present([_assistant(("c1", "agent"))], events=events)[1]["content"]
    assert "sub-done" not in content


# ── 端到端：从事件流派生出来的消息必须是合法形状 ───────────────────

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


def test_session_with_dangling_call_derives_valid_shape(session_env):
    """400 的现场复现：assistant_message 带 tool_calls 落盘、工具没跑完进程就没了。
    重载之后派生出的消息必须自带结果，否则这个会话永远续不下去。"""
    from agents.core.session import Session

    s = Session(session_id="s-dangling")
    s.append("user_message", {"content": "跑一下测试"})
    s.append("assistant_message", {
        "content": "",
        "tool_calls": [{
            "id": "call_deadbeef",
            "type": "function",
            "function": {"name": "run_shell", "arguments": '{"command":"pytest"}'},
        }],
    })
    # 刻意不写 tool_result_msg —— 这就是硬杀留下的形状

    msgs = s.get_messages_for_llm()
    dangling = [
        tc["id"]
        for m in msgs if m.get("role") == "assistant"
        for tc in m.get("tool_calls") or []
        if tc["id"] not in _answered_ids(msgs)
    ]
    assert not dangling, f"派生结果里仍有无结果的 tool_call：{dangling}"
