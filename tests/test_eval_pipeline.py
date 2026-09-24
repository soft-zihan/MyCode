"""评估管道单测（Phase 3）— code evaluators / judge 解析 / 失败回流。

全部使用 fake bundle 与 fake client，不发网络请求。
"""

from __future__ import annotations

import pytest

from eval.langfuse.code_evaluators import (
    evaluate_bundle,
    eval_event_range_complete,
    eval_repeated_tool_calls,
    eval_subagent_timeout_loop,
    eval_tool_success,
)
from eval.langfuse.failure_classifier import FailureClassifier, FailureMode
from eval.langfuse.judge import parse_judge_response, _tool_sequence_text, _truncate_middle


def _obs(type_: str, name: str, level: str = "DEFAULT", input=None, output=None, metadata: dict | None = None, start: str = "") -> dict:
    return {
        "type": type_,
        "name": name,
        "level": level,
        "input": input,
        "output": output,
        "startTime": start,
        "metadata": metadata or {},
    }


def _bundle(observations: list[dict], input=None, output=None) -> dict:
    return {"id": "t1", "input": input, "output": output, "observations": observations}


class TestCodeEvaluators:
    def test_tool_success_ratio(self):
        bundle = _bundle([
            _obs("TOOL", "read_file"),
            _obs("TOOL", "run_shell", level="ERROR"),
            _obs("TOOL", "grep_search"),
            _obs("TOOL", "edit_file"),
        ])
        r = eval_tool_success(bundle)
        assert r["value"] == 0.75
        assert r["data_type"] == "NUMERIC"

    def test_tool_success_skips_without_tools(self):
        assert eval_tool_success(_bundle([_obs("CHAIN", "turn")])) is None

    def test_repeated_detection(self):
        same = lambda: _obs("TOOL", "read_file", input='{"path": "a.py"}')
        bundle = _bundle([same(), same(), same()])
        r = eval_repeated_tool_calls(bundle)
        assert r["value"] is True
        assert "x3" in r["comment"]

    def test_repeated_no_false_positive(self):
        bundle = _bundle([
            _obs("TOOL", "read_file", input='{"path": "a.py"}'),
            _obs("TOOL", "read_file", input='{"path": "b.py"}'),
            _obs("TOOL", "read_file", input='{"path": "a.py"}'),
        ])
        r = eval_repeated_tool_calls(bundle)
        assert r["value"] is False

    def test_subagent_timeout_loop_repeated_prompt(self):
        same = lambda: _obs(
            "AGENT",
            "agent.general",
            level="ERROR",
            input="same long task",
            output="Sub-agent timed out after 900s",
            metadata={"status": "timeout", "outcome": "timeout", "tool_call_count": 0, "failed_tool_call_count": 0},
        )
        r = eval_subagent_timeout_loop(_bundle([same(), same()]))
        assert r["value"] is True
        assert "timeouts=2" in r["comment"]

    def test_subagent_timeout_with_progress_not_looping(self):
        obs = lambda i: _obs(
            "AGENT",
            "agent.general",
            level="ERROR",
            input=f"different task {i}",
            output="timeout",
            metadata={"status": "timeout", "outcome": "timeout", "tool_call_count": 8, "failed_tool_call_count": 1, "child_turn_count": 2},
        )
        r = eval_subagent_timeout_loop(_bundle([obs(1), obs(2)]))
        assert r["value"] is False
        assert "progressing_timeouts=2" in r["comment"]

    def test_event_range_complete(self):
        bundle = _bundle([_obs("CHAIN", "turn", metadata={
            "turn_id": "s:1",
            "event_range_start_seq": 1,
            "event_range_end_seq": 42,
        })])
        assert eval_event_range_complete(bundle)["value"] is True

    def test_event_range_incomplete(self):
        bundle = _bundle([_obs("CHAIN", "turn", metadata={"turn_id": "s:1", "event_range_start_seq": 1})])
        assert eval_event_range_complete(bundle)["value"] is False

    def test_evaluate_bundle_aggregates(self):
        bundle = _bundle([
            _obs("CHAIN", "turn", metadata={"turn_id": "s:1", "event_range_start_seq": 1, "event_range_end_seq": 9}),
            _obs("TOOL", "read_file", input="{}"),
        ])
        names = {r["name"] for r in evaluate_bundle(bundle)}
        assert names == {"tool_success", "repeated_tool_calls", "event_range_complete"}


class TestFailureClassifier:
    def test_subagent_timeout_loop_is_classified(self):
        obs = _obs(
            "AGENT",
            "agent.general",
            level="ERROR",
            input="same task",
            output="timed out",
            metadata={"status": "timeout", "outcome": "timeout", "tool_call_count": 0},
        )
        bundle = _bundle([obs, obs], input="question", output="")
        result = FailureClassifier().classify(bundle, "trace-1")
        assert result.is_failure
        assert result.primary_mode == FailureMode.SUBAGENT_LOOP
        assert any("重复超时" in ev for ev in result.classifications[0].evidence)


class TestEvaluateTraceWithFakeClient:
    def test_evaluate_trace_posts_scores(self):
        from eval.langfuse.code_evaluators import evaluate_trace

        class FakeClient:
            def __init__(self):
                self.posted = []

            def fetch_trace(self, trace_id):
                return _bundle([_obs("TOOL", "read_file")], input="hi")

            def create_score(self, **kwargs):
                self.posted.append(kwargs)
                return {"id": "score-1"}

        client = FakeClient()
        results = evaluate_trace(client, "t1")
        assert len(results) == len(client.posted) == 2  # tool_success + repeated
        assert all(p["trace_id"] == "t1" for p in client.posted)


class TestJudgeParsing:
    def test_parse_plain_json(self):
        r = parse_judge_response('{"score": 0.8, "reasoning": "ok"}')
        assert r["score"] == 0.8 and r["reasoning"] == "ok"

    def test_parse_fenced_json(self):
        r = parse_judge_response('blah\n```json\n{"score": 1.0, "reasoning": "done"}\n```\nbye')
        assert r["score"] == 1.0

    def test_parse_clamps_range(self):
        assert parse_judge_response('{"score": 5, "reasoning": ""}')["score"] == 1.0
        assert parse_judge_response('{"score": -2, "reasoning": ""}')["score"] == 0.0

    def test_parse_invalid_raises(self):
        with pytest.raises(ValueError):
            parse_judge_response("no json here")

    def test_tool_sequence_sorted(self):
        bundle = _bundle([
            _obs("TOOL", "b_tool", input="{}", start="2026-01-01T00:00:02Z"),
            _obs("TOOL", "a_tool", level="ERROR", input="{}", start="2026-01-01T00:00:01Z"),
            _obs("CHAIN", "turn", start="2026-01-01T00:00:00Z"),
        ])
        text = _tool_sequence_text(bundle)
        assert text.index("a_tool") < text.index("b_tool")
        assert "ERROR" in text

    def test_tool_sequence_keeps_head_tail_and_failures(self):
        observations = []
        for i in range(50):
            level = "ERROR" if i == 25 else "DEFAULT"
            observations.append(_obs(
                "TOOL",
                f"tool_{i}",
                level=level,
                input="{}",
                start=f"2026-01-01T00:00:{i:02d}Z",
            ))
        text = _tool_sequence_text(_bundle(observations), head_items=5, tail_items=5, failure_items=5)
        assert "total_tool_calls=50" in text
        assert "tool_0 [ok]" in text
        assert "tool_25 [ERROR]" in text
        assert "tool_49 [ok]" in text
        assert "\n20. tool_20 [ok]" not in text

    def test_truncate_middle_preserves_final_answer(self):
        text = _truncate_middle("HEAD" + "x" * 10000 + "FINAL ANSWER: 65", head_chars=10, tail_chars=20)
        assert text.startswith("HEAD")
        assert text.endswith("FINAL ANSWER: 65")
        assert "truncated" in text


class TestDatasetSync:
    def test_export_failures(self):
        from eval.langfuse.dataset_sync import export_failures_to_dataset, classify_failure

        err_bundle = _bundle(
            [_obs("TOOL", "run_shell", level="ERROR")],
            input="run something",
        )
        ok_bundle = _bundle([_obs("TOOL", "read_file")], input="read something")

        class FakeClient:
            def __init__(self):
                self.items = []
                self.datasets = []

            def create_dataset(self, name, description=""):
                self.datasets.append(name)
                return {"name": name}

            def fetch_traces(self, limit=50, from_timestamp=None, **kw):
                return [{"id": "err-trace"}, {"id": "ok-trace"}]

            def fetch_trace(self, trace_id):
                return dict(err_bundle, id=trace_id) if trace_id == "err-trace" else dict(ok_bundle, id=trace_id)

            def upsert_dataset_item(self, **kwargs):
                self.items.append(kwargs)
                return {"id": kwargs.get("id")}

        client = FakeClient()
        result = export_failures_to_dataset(client, limit=10)
        assert result["scanned"] == 2
        assert result["exported"] == 1
        assert client.items[0]["item_id"] == "trace-err-trace"
        assert "error_obs" in client.items[0]["metadata"]["failure_reasons"][0]

    def test_classify_failure_by_scores(self):
        from eval.langfuse.dataset_sync import classify_failure

        bundle = _bundle([])
        assert classify_failure(bundle, [{"name": "tool_success", "value": 0.5}]) == ["tool_success=0.5"]
        assert classify_failure(bundle, [{"name": "repeated_tool_calls", "value": True}]) == ["repeated_tool_calls"]
        assert classify_failure(bundle, [{"name": "tool_success", "value": 1.0}]) == []


class TestSelectMainTraces:
    """BC-24：评审对象筛选——派生 trace 不按父任务标准评审。"""

    def test_excludes_session_title_and_sub_agent(self):
        from eval.langfuse.run_evals import select_main_traces

        traces = [
            {"id": "t1", "name": "agent-turn", "tags": ["main-agent"]},
            {"id": "t2", "name": "session-title", "tags": []},
            {"id": "t3", "name": "agent-turn", "tags": ["sub-agent"]},
            {"id": "t4", "name": "smoke-eval", "tags": ["eval", "smoke"]},
            {"id": "t5", "name": "agent-turn", "tags": ["main-agent", "plan-mode"]},
            # 真实数据形态：side query 在子代理上下文自开 trace，name 仍是 agent-turn
            {"id": "t6", "name": "agent-turn", "tags": ["session-title", "side-query", "sub-agent"]},
            {"id": "t7", "name": "agent-turn", "tags": ["main-agent", "side-query"]},
        ]
        ids, excluded = select_main_traces(traces)
        assert ids == ["t1", "t4", "t5"]
        assert excluded == {"session-title": 1, "sub-agent": 2, "side-query": 1}

    def test_no_exclusions_returns_all_and_empty_counts(self):
        from eval.langfuse.run_evals import select_main_traces

        traces = [{"id": "t1", "name": "agent-turn", "tags": ["main-agent"]}]
        ids, excluded = select_main_traces(traces)
        assert ids == ["t1"]
        assert excluded == {}

    def test_missing_id_skipped(self):
        from eval.langfuse.run_evals import select_main_traces

        ids, _ = select_main_traces([{"name": "agent-turn", "tags": []}])
        assert ids == []
