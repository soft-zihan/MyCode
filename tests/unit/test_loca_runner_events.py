"""loca 的行字典字段和 harbor 不同（compression_events / step_info），但状态口径必须一样。

Task 9 起 loca 不再有自己的那份行映射（`_unified_row` 已删）：事件流和报告走的是同一份
`eval.common.schema.normalize_row`，所以这里钉的是**它**对 loca 行的判定。

下面 5 条夹具的 `reward` 要么有值、要么为 None，没有"reward 有值 + 有 error + passed=False"
那种行——那种行上 `_unified_row`（error 先判）和 `schema._status`（failed 先判）会给出不同
答案，按口径裁定 2 以 `schema._status` 为准（harbor 的 cpp 构建失败就是那种行，它是做错了，
不是没跑起来）。所以这 5 条的期望值在换掉入口之后**一个都不用改**。
"""

from __future__ import annotations

from eval.common.schema import normalize_row


def _row(**over):
    base = {
        "task_id": "final-3", "name": "cfg", "index": 3, "reward": 1.0, "passed": True,
        "duration_s": 12.5, "tokens": {"input": 100, "output": 20},
        "compression_events": {}, "trace_id": "tr", "session_id": "se",
        "agent_error": None, "eval_error": None, "step_info": None, "task_dir": "/tmp/x",
    }
    base.update(over)
    return base


def _unified(**over):
    # 传进去的必须是原始 row：normalize_row 的快速路径（带齐 11 个核心键就绕过适配器）
    # 会跳过 loca 适配器里"reward is None → passed=None"那条规则
    return normalize_row(_row(**over), benchmark="loca")


def test_a_passed_row_stays_passed():
    out = _unified()
    assert out["status"] == "passed" and out["passed"] is True
    # loca 独有的字段进 extra，不挤掉通用核心。按键断言：normalize_row 的 extra 是
    # "凡不在 ROW_CORE 里的原始键全收"，比 _unified_row 手挑的四个键更多
    assert out["extra"]["compression_events"] == {}
    assert out["extra"]["step_info"] is None
    assert out["extra"]["index"] == 3
    assert out["extra"]["task_dir"] == "/tmp/x"


def test_a_scored_failure_is_failed_not_error():
    out = _unified(reward=0.4, passed=False)
    assert out["status"] == "failed" and out["passed"] is False


def test_an_error_row_is_error():
    out = _unified(reward=None, passed=False, agent_error="boom")
    assert out["status"] == "error" and out["passed"] is None
    assert out["error"] == "boom"


def test_no_reward_and_no_error_is_unscored_not_failed():
    # verifier 没产出 reward：没判分 ≠ 判了没过。混成 failed 会白白压低通过率
    out = _unified(reward=None, passed=False)
    assert out["status"] == "unscored" and out["passed"] is None


def test_eval_error_wins_when_there_is_no_agent_error():
    out = _unified(reward=None, passed=False, eval_error="side rc=1")
    assert out["status"] == "error" and out["error"] == "side rc=1"
