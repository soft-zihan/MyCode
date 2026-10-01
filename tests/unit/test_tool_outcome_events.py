"""tool_result_msg 必须落盘 outcome 与 tool_name —— 验收闸门的事实来源。

闸门要事后回答「这条任务在做的时候有没有跑过**成功的 shell 命令**」。只有
outcome 无法区分是哪个工具，只有 tool_name 无法区分成败，所以两个键都要；
工具名此前只能从 <tool_result tool="..."> 的包裹文本里解析，脆弱。
"""
from __future__ import annotations

import pytest

from agents.core.context import ContextManager
from agents.core.session import Session


@pytest.fixture(autouse=True)
def session_env(tmp_path, monkeypatch):
    """重定向 MYCODE_SESSION_DIR 并复位全局 backend / 投影缓存。

    与 tests/unit/test_event_manifest.py 的 session_env 同一约定：两个 backend 都
    在**每次访问**时解析 session_dir()（全局单例不固化第一个调用方的目录），复位
    _backend 则让 MYCODE_SESSION_BACKEND 在本模块内被重新读取——jsonl 与 sqlite
    两条腿跑的是同一份断言。
    """
    monkeypatch.setenv("MYCODE_SESSION_DIR", str(tmp_path / "sessions"))
    import agents.core.session as sess
    import agents.core.session_projection_cache as spc

    monkeypatch.setattr(sess, "_backend", None)
    monkeypatch.setattr(spc, "_projection_cache", None)
    yield tmp_path
    monkeypatch.setattr(sess, "_backend", None)
    monkeypatch.setattr(spc, "_projection_cache", None)


@pytest.fixture
def session():
    s = Session("outcome-s", origin="sub_agent")
    s.system_prompt = "SYS"
    return s


class _Stub:
    """ContextManager 的最小 agent 替身。

    ContextManager.__init__ 只收关键字参数 agent_ref 并存成 self.agent；本文件
    只调用 append_tool_message，而它唯一触达的属性是 self.agent.session（其余
    方法触达的 _system_prompt / _compressor / _session_lifecycle 等这里都不经过）。
    结果字符计数 _tool_result_chars 属于 Agent 的包装方法，不在 ContextManager 上，
    所以替身不需要它。
    """

    def __init__(self, session):
        self.session = session


def _cm(session) -> ContextManager:
    return ContextManager(agent_ref=_Stub(session))


def _results(session):
    return [e for e in session.events if e.get("type") == "tool_result_msg"]


def test_records_outcome_and_tool_name(session):
    _cm(session).append_tool_message("c1", "输出", "run_shell", "success")
    ev = _results(session)[0]
    assert ev["tool_name"] == "run_shell"
    assert ev["outcome"] == "success"
    assert ev["call_id"] == "c1"


def test_content_still_wrapped_in_tool_result_tag(session):
    _cm(session).append_tool_message("c1", "输出", "run_shell", "success")
    assert _results(session)[0]["content"] == '<tool_result tool="run_shell">\n输出\n</tool_result>'


def test_outcome_defaults_to_empty_for_cancel_paths(session):
    """取消/中止路径不传新参数，必须落空串而不是缺键或 None。"""
    _cm(session).append_tool_message("c1", "输出")
    ev = _results(session)[0]
    assert ev["outcome"] == ""
    assert ev["tool_name"] == ""


def test_error_outcome_is_preserved_not_normalized(session):
    _cm(session).append_tool_message("c1", "boom", "run_shell", "error")
    assert _results(session)[0]["outcome"] == "error"


def test_new_fields_do_not_break_message_derivation(session):
    """新字段不得改变派生：tool_result_msg 仍然只产出一条 role=tool。"""
    s = session
    s.append("assistant_message", {"content": "", "tool_calls": [
        {"id": "c1", "type": "function",
         "function": {"name": "run_shell", "arguments": "{}"}}]})
    _cm(s).append_tool_message("c1", "输出", "run_shell", "success")
    msgs = s.get_messages_for_llm()
    assert [m["role"] for m in msgs] == ["system", "assistant", "tool"]
    assert msgs[-1]["tool_call_id"] == "c1"
    assert "outcome" not in msgs[-1]      # 事件字段不得泄漏进 LLM 消息


def test_new_fields_survive_persistence(session):
    """outcome/tool_name 必须真的落盘，不能只在内存里。

    走 Session.load_from_events 的完整重载路径（含 crash recovery 与投影重放），
    两个 backend 都把事件整体存成 JSON，所以新键不需要 schema 迁移。
    """
    _cm(session).append_tool_message("c1", "输出", "run_shell", "success")
    loaded = Session.load_from_events("outcome-s")
    assert loaded is not None
    ev = [e for e in loaded.events if e.get("type") == "tool_result_msg"][0]
    assert ev["outcome"] == "success"
    assert ev["tool_name"] == "run_shell"
