"""harbor 的报告要能被通用那套读出来：run_id 就是文件名主干，行是标准核心 + extra。

钉住的是「harbor 不再是特例」这件事——它一旦特例，前端就要为它单写一套渲染，
对比层也没法把它和 gaia/loca 放一起比，而这正是本次要解决的问题。
"""

from __future__ import annotations

import json

import pytest

from eval.common.schema import check_report, looks_like_run_report
from eval.harbor.runner import collect_trials, render_markdown, summarize, write_report


def _write_trial(root, name: str, data: dict) -> None:
    trial_dir = root / name
    trial_dir.mkdir(parents=True, exist_ok=True)
    (trial_dir / "result.json").write_text(json.dumps(data), encoding="utf-8")


@pytest.fixture
def trials(tmp_path):
    # 时间戳**必须带小数秒**：`_phase_seconds` 用 "%Y-%m-%dT%H:%M:%S.%fZ" 解析，
    # `%f` 是必需位，写成 "02:54:00Z" 会 ValueError → 返回 None（已实测）。
    # 真 harbor 的 result.json 带小数秒，所以生产路径没问题，是这份夹具要照抄。
    # agent_setup / verifier 两个相位也必须给：下面有两条断言要它们在 extra 里非 None，
    # 而 `_phase_seconds(None)` 返回 None（已实测）。
    _write_trial(tmp_path, "polyglot_cpp_yacht__1", {
        "task_name": "polyglot_cpp_yacht",
        "verifier_result": {"rewards": {"reward": 1.0}},
        "agent_result": {"n_input_tokens": 47582, "n_output_tokens": 1405, "n_cache_tokens": 0,
                         "metadata": {"mycode_reasoning_tokens": 308, "mycode_session_id": "5dc19fa4"}},
        "agent_setup": {"started_at": "2026-10-18T02:53:50.000Z", "finished_at": "2026-10-18T02:54:00.000Z"},
        "agent_execution": {"started_at": "2026-10-18T02:54:00.000Z", "finished_at": "2026-10-18T02:54:30.000Z"},
        "verifier": {"started_at": "2026-10-18T02:54:30.000Z", "finished_at": "2026-10-18T02:54:34.500Z"},
    })
    # 未判分的那一题：跑了但没结论，不能被算成失败。
    # NonZeroAgentExitCodeError **不在** NO_REWARD_FILE_EXCEPTIONS（那个集合只有
    # RewardFileNotFoundError），所以 reward 保持 None → passed=None → status="error"
    _write_trial(tmp_path, "polyglot_go_scale-generator__1", {
        "task_name": "polyglot_go_scale-generator",
        "exception_info": {"exception_type": "NonZeroAgentExitCodeError", "exception_message": "exit 1"},
    })
    return collect_trials(tmp_path)


@pytest.fixture
def reports(tmp_path, monkeypatch):
    out = tmp_path / "reports"
    monkeypatch.setattr("eval.harbor.runner.REPORTS_DIR", out)
    return out


def _write(reports, trials, **over):
    kwargs = dict(
        arm="mycode", stage="final", model="qwen3.6-27b", trials=trials,
        run_dirs=["/home/han/.mycode/harbor_jobs/2026-10-18__02-53-52"],
        tasks=[t["task_name"] for t in trials], seed=20261017,
        stamp="20261018__025351",
        variables={"model": "qwen3.6-27b", "thinking_mode": "default", "seed": 20261017},
    )
    kwargs.update(over)
    return write_report(**kwargs)


def test_the_json_is_a_standard_envelope(reports, trials):
    _write(reports, trials)

    report = json.loads((reports / "harbor_mycode_final_20261018__025351.json").read_text(encoding="utf-8"))

    assert check_report(report) == []
    assert looks_like_run_report(report)
    assert report["benchmark"] == "harbor"


def test_run_id_is_the_file_stem_so_the_reports_api_can_find_it(reports, trials):
    _write(reports, trials)

    report = json.loads((reports / "harbor_mycode_final_20261018__025351.json").read_text(encoding="utf-8"))

    # /api/eval/reports/{run_id} 就是按 f"{run_id}.json" 找的；对上这一个，harbor 就不再 404
    assert report["run_id"] == "harbor_mycode_final_20261018__025351"
    assert (reports / f"{report['run_id']}.json").exists()
    assert (reports / f"{report['run_id']}.md").exists()


def test_rows_use_the_common_core_and_keep_harbor_details_in_extra(reports, trials):
    _write(reports, trials)

    report = json.loads((reports / "harbor_mycode_final_20261018__025351.json").read_text(encoding="utf-8"))
    by_id = {r["task_id"]: r for r in report["results"]}

    yacht = by_id["polyglot_cpp_yacht"]
    assert yacht["passed"] is True and yacht["status"] == "passed"
    assert yacht["tokens"] == {"input": 47582, "output": 1405, "cached": 0, "reasoning": 308}
    assert yacht["duration_s"] == pytest.approx(30.0, abs=0.5)
    assert yacht["session_id"] == "5dc19fa4"
    # harbor 特有的相位耗时是它最有价值的部分（装 agent 和跑题量级不同），不能丢。
    # 断具体值而不是 `is not None`：`is not None` 连"解析失败返回 None 被兜成 0"都测不出来
    assert yacht["extra"]["agent_setup_s"] == pytest.approx(10.0)
    assert yacht["extra"]["verifier_s"] == pytest.approx(4.5)
    assert yacht["extra"]["no_reward_file"] is False

    unscored = by_id["polyglot_go_scale-generator"]
    assert unscored["passed"] is None and unscored["status"] == "error"
    assert "NonZeroAgentExitCodeError" in unscored["error"]


def test_summary_keeps_the_harbor_keys_and_gains_the_common_ones(reports, trials):
    _write(reports, trials)

    summary = json.loads(
        (reports / "harbor_mycode_final_20261018__025351.json").read_text(encoding="utf-8")
    )["summary"]

    # render_markdown 和 compare 读的是这些旧键，必须还在
    assert summary["arm"] == "mycode" and summary["stage"] == "final"
    assert summary["n_passed"] == 1 and summary["n_scored"] == 1
    # 公共核心是加上去的，n_unscored 由此有了跨 benchmark 一致的语义
    assert summary["n_tasks"] == 2 and summary["n_unscored"] == 1
    assert summary["pass_rate"] == 1.0
    assert summary["totals"]["reasoning_tokens"] == 308


def test_meta_carries_the_control_variables(reports, trials):
    _write(reports, trials)

    meta = json.loads(
        (reports / "harbor_mycode_final_20261018__025351.json").read_text(encoding="utf-8")
    )["meta"]

    assert meta["model"] == "qwen3.6-27b" and meta["arm"] == "mycode"
    assert meta["seed"] == 20261017
    assert meta["variables"]["thinking_mode"] == "default"
    assert meta["task_ids"] == ["polyglot_cpp_yacht", "polyglot_go_scale-generator"]
    # 没有它，两次运行没法当基线对比——这就是上一轮假结论活到汇报环节的原因
    assert meta["git_revision"]


def test_the_markdown_still_renders_from_the_raw_trials(reports, trials):
    json_path = _write(reports, trials)
    # `write_report` 从 Task 5 起返回的是 **.json**（调用方要拿它当 `report_json_path`
    # 发进 eval/run_finished，而 finish_job 也是按 <run_id>.json 找报告）。
    # 人看的那份是同主干的 .md，得自己换后缀——直接 read_text 返回值读到的是 JSON
    md_path = json_path.with_suffix(".md")

    text = md_path.read_text(encoding="utf-8")

    assert "这轮的控制变量" in text
    assert "| polyglot_cpp_yacht |" in text
    # 未判分那题在 md 里要看得出来，不能和"做错了"长一个样
    assert "polyglot_go_scale-generator" in text
    assert "未判分" in text


def test_render_markdown_takes_the_normalized_summary(reports, trials):
    # write_report 传给 render_markdown 的是归一化后的 summary（旧键的超集），
    # 所以这里必须能直接吃 normalize_report 的产物
    from eval.common.schema import normalize_report

    summary = summarize("mycode", "final", trials)
    summary["variables"] = {"thinking_mode": "default"}
    normalized = normalize_report(
        {"benchmark": "harbor", "run_id": "r", "generated_at": "t",
         "meta": {}, "summary": summary, "results": trials}
    )

    assert "这轮的控制变量" in render_markdown(normalized["summary"], trials)
