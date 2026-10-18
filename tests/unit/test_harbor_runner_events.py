"""harbor 的事件：它跑完才有结果，所以事件在收集阶段补发，进度靠 run_progress。"""

from __future__ import annotations

import json

from eval.harbor.runner import emit_trial_events


class _ListSink:
    def __init__(self):
        self.events: list[dict] = []

    def emit(self, type, **payload):
        self.events.append({"type": type, **payload})


def _trial(name, reward, exec_s=10.0, inp=100, out=10, reasoning=0):
    return {
        "task_name": name,
        "reward": reward,
        "no_reward_file": False,
        "exception_type": None,
        "agent_execution_s": exec_s,
        "agent_setup_s": 5.0,
        "verifier_s": 1.0,
        "n_input_tokens": inp,
        "n_output_tokens": out,
        "n_cache_tokens": 0,
        "agent_metadata": {"mycode_reasoning_tokens": reasoning},
    }


def test_emits_one_task_finished_per_trial():
    sink = _ListSink()

    emit_trial_events(sink, "mycode", ["a", "b"], [_trial("a", 1.0), _trial("b", 0.0)])

    finished = [e for e in sink.events if e["type"] == "eval/task_finished"]
    assert [e["task_id"] for e in finished] == ["a", "b"]
    assert [e["result"]["passed"] for e in finished] == [True, False]


def test_task_finished_carries_the_unified_result_shape():
    sink = _ListSink()

    emit_trial_events(sink, "mycode", ["a"], [_trial("a", 1.0, inp=500, out=40, reasoning=15)])

    result = sink.events[0]["result"]
    assert result["task_id"] == "a"
    assert result["tokens"] == {"input": 500, "output": 40, "reasoning": 15, "cached": 0}
    assert result["duration_s"] == 10.0
    assert result["extra"]["agent_setup_s"] == 5.0


def test_emits_progress_with_eta_after_each_trial():
    sink = _ListSink()

    emit_trial_events(sink, "mycode", ["a", "b", "c"],
                      [_trial("a", 1.0, exec_s=10.0), _trial("b", 0.0, exec_s=20.0)])

    progress = [e for e in sink.events if e["type"] == "eval/run_progress"]
    assert [e["completed"] for e in progress] == [1, 2]
    assert all(e["total"] == 3 for e in progress)
    # ETA 用已完成题目的中位耗时估算，不是均值——harbor 的耗时分布很偏
    assert progress[-1]["eta_s"] > 0


def test_no_trials_emits_nothing_but_is_not_an_error():
    sink = _ListSink()

    emit_trial_events(sink, "mycode", [], [])

    assert sink.events == []


def test_unscored_trial_is_reported_as_such():
    # reward 为 None 表示出异常没判分，不能当成"没过"
    sink = _ListSink()
    trial = _trial("a", None)
    trial["exception_type"] = "AgentSetupTimeoutError"

    emit_trial_events(sink, "mycode", ["a"], [trial])

    result = sink.events[0]["result"]
    assert result["passed"] is None
    assert result["status"] == "error"
    assert result["error"] == "AgentSetupTimeoutError"


def test_a_trial_with_no_reward_and_no_exception_is_unscored_not_failed():
    """verifier 没产出 reward 文件：没判分 ≠ 判了没过。

    混成 failed 会直接压低通过率——harbor 那次 25 题里就有 1 题是这种
    （`polyglot_go_scale-generator`，NonZeroAgentExitCodeError 之外还有 no_reward_file 的情况）。
    """
    sink = _ListSink()
    trial = _trial("a", None)
    trial["no_reward_file"] = True

    emit_trial_events(sink, "mycode", ["a"], [trial])

    result = sink.events[0]["result"]
    assert result["passed"] is None
    assert result["status"] == "unscored"
    assert result["extra"]["no_reward_file"] is True
