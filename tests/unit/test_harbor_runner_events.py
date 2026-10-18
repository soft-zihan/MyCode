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

    # 耗时刻意选成中位数 ≠ 均值：median([10,20,120]) = 20.0，mean = 50.0。
    # tasks 给了 4 道、只跑完 3 道 → remaining = 1，于是最后一条 eta_s 只有中位数
    # 能算出 20.0，用均值会是 50.0。（前两条钉不住这个区别：[10] → 10.0×3 = 30.0，
    # [10,20] → 15.0×2 = 30.0，中位与均值同解。）
    emit_trial_events(sink, "mycode", ["a", "b", "c", "d"],
                      [_trial("a", 1.0, exec_s=10.0), _trial("b", 0.0, exec_s=20.0),
                       _trial("c", 1.0, exec_s=120.0)])

    progress = [e for e in sink.events if e["type"] == "eval/run_progress"]
    assert [e["completed"] for e in progress] == [1, 2, 3]
    assert all(e["total"] == 4 for e in progress)
    # ETA 用已完成题目的中位耗时估算，不是均值——harbor 的耗时分布很偏
    assert [e["eta_s"] for e in progress] == [30.0, 30.0, 20.0]


def test_no_trials_emits_nothing_but_is_not_an_error():
    sink = _ListSink()

    emit_trial_events(sink, "mycode", [], [])

    assert sink.events == []


def test_a_trial_that_raised_is_error_not_unscored():
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
    """verifier 结果里没有 reward、也没有异常：没判分 ≠ 判了没过。

    可达路径只有一条：`collect_trials` 读到的 result.json 里 `verifier_result.rewards`
    没有 `reward` 键，而 `exception_info.exception_type` 为空（或不在
    `NO_REWARD_FILE_EXCEPTIONS` 里）。**别用 `no_reward_file=True` 造这个 fixture**——
    见 `collect_trials` 里 `no_reward_file` → `reward = 0.0` 那段强制转换，以及
    `NO_REWARD_FILE_EXCEPTIONS` 上方的政策注释：`reward is None` 且 `no_reward_file`
    为真这个组合从真实数据里根本到不了这里。

    那条政策是刻意的，别顺手"修"掉：aider-polyglot 的 cpp `tests/test.sh` 在 CMake
    构建失败时直接 `exit 1`、跳过写 reward.txt，harbor 于是抛 RewardFileNotFoundError；
    但 C++ 里「构建不过」几乎总是 agent 给的 API 对不上测试文件，那是**做错了**，
    必须按 reward 0 计分。把它改成 unscored 等于剔掉难题，做得差的那一臂通过率反而虚高
    （`eval/harbor/README.md` §5.7 那个 13/20 vs 8/20 的结论就建在这条口径上）。
    """
    sink = _ListSink()
    # no_reward_file 保持 fixture 默认的 False：这才是真实数据能走到的那条路
    trial = _trial("a", None)

    emit_trial_events(sink, "mycode", ["a"], [trial])

    result = sink.events[0]["result"]
    assert result["passed"] is None
    assert result["status"] == "unscored"


def test_write_report_returns_the_json_path_and_still_writes_the_md(monkeypatch, tmp_path):
    """本任务对既有代码唯一的行为改动：`write_report` 现在返回 `.json` 而不是 `.md`。

    job 索引和 `/api/eval/reports/{id}` 吃的都是 JSON 报告，`finish_job` 更是直接按
    `<run_id>.json` 找报告——返回 .md 的话一轮成功的评测会被判成 FAILED。
    人看的那份 .md 仍然要写（print 里显示的是它），所以两个都断言。
    """
    from eval.harbor import runner

    monkeypatch.setattr(runner, "REPORTS_DIR", tmp_path)

    report = runner.write_report(
        arm="mycode",
        stage="probe",
        model="qwen3.6-27b",
        trials=[_trial("a", 1.0)],
        run_dirs=[tmp_path / "job"],
        tasks=["a"],
        seed=20261017,
        stamp="20261018__000000",
    )

    assert report.parent == tmp_path  # 落在 tmp_path，没有污染 eval/reports/
    assert report.suffix == ".json"
    assert report.exists()
    assert report.with_suffix(".md").exists()
