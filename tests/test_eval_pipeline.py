"""评估管道单测（Phase 3）— code evaluators / judge 解析 / 失败回流。

全部使用 fake bundle 与 fake client，不发网络请求。
"""

from __future__ import annotations

import pytest

from eval.langfuse.code_evaluators import (
    evaluate_bundle,
    eval_event_range_complete,
    eval_repeated_tool_calls,
    eval_tool_success,
)
from eval.langfuse.judge import parse_judge_response, _tool_sequence_text


def _obs(type_: str, name: str, level: str = "DEFAULT", input=None, metadata: dict | None = None, start: str = "") -> dict:
    return {
        "type": type_,
        "name": name,
        "level": level,
        "input": input,
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
