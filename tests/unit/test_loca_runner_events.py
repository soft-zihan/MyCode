"""loca 的行字典字段和 harbor 不同（compression_events / step_info），但状态口径必须一样。"""

from __future__ import annotations

from eval.loca.runner import _unified_row


def _row(**over):
    base = {
        "task_id": "final-3", "name": "cfg", "index": 3, "reward": 1.0, "passed": True,
        "duration_s": 12.5, "tokens": {"input": 100, "output": 20},
        "compression_events": {}, "trace_id": "tr", "session_id": "se",
        "agent_error": None, "eval_error": None, "step_info": None, "task_dir": "/tmp/x",
    }
    base.update(over)
    return base


def test_a_passed_row_stays_passed():
    out = _unified_row(_row())
    assert out["status"] == "passed" and out["passed"] is True
    # loca 独有的字段进 extra，不挤掉通用核心
    assert out["extra"]["compression_events"] == {}
    assert out["extra"]["step_info"] is None


def test_a_scored_failure_is_failed_not_error():
    out = _unified_row(_row(reward=0.4, passed=False))
    assert out["status"] == "failed" and out["passed"] is False


def test_an_error_row_is_error():
    out = _unified_row(_row(reward=None, passed=False, agent_error="boom"))
    assert out["status"] == "error" and out["passed"] is None
    assert out["error"] == "boom"


def test_no_reward_and_no_error_is_unscored_not_failed():
    # verifier 没产出 reward：没判分 ≠ 判了没过。混成 failed 会白白压低通过率
    out = _unified_row(_row(reward=None, passed=False))
    assert out["status"] == "unscored" and out["passed"] is None


def test_eval_error_wins_when_there_is_no_agent_error():
    out = _unified_row(_row(reward=None, passed=False, eval_error="side rc=1"))
    assert out["status"] == "error" and out["error"] == "side rc=1"
