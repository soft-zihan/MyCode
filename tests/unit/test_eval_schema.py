"""统一记录：五个 benchmark 的行字典归一化成同一份公共核心 + extra。"""

from __future__ import annotations

import pytest

from eval.common.schema import (
    ENVELOPE_KEYS,
    META_CORE,
    ROW_CORE,
    SUMMARY_CORE,
    TOKEN_FIELDS,
    check_report,
    looks_like_run_report,
    normalize_report,
    normalize_row,
    summarize_rows,
)

TASK_RESULT = {  # EvalTaskResult.to_dict() 的形状（gaia/hle/smoke）
    "task_id": "gaia-1",
    "benchmark": "gaia",
    "name": "哪一年…",
    "status": "passed",
    "expected": "2019",
    "predicted": "2019",
    "correct": True,
    "passed": None,
    "duration_s": 42.5,
    "tokens": {"input": 1200, "output": 300, "reasoning": 40, "cached": 100},
    "trace_id": "tr-1",
    "session_id": "s-1",
    "error": None,
    "langfuse_dataset": {"synced": True},
    "metadata": {"level": 3},
}

LOCA_ROW = {
    "task_id": "64k-3",
    "name": "config_3",
    "index": 3,
    "reward": 0.75,
    "passed": False,
    "duration_s": 88.0,
    "tokens": {"input": 900, "output": 200},
    "compression_events": {"tool_folded": 2, "session_folded": 0, "events_hidden": 5},
    "trace_id": "tr-2",
    "session_id": "s-2",
    "agent_error": None,
    "eval_error": "verifier timeout",
    "step_info": {"steps": 12},
    "task_dir": "/tmp/x",
}

HARBOR_TRIAL = {
    "task_name": "polyglot_cpp_yacht",
    "trial_name": "polyglot_cpp_yacht__ibzbDgc",
    "reward": 1.0,
    "no_reward_file": False,
    "exception_type": None,
    "exception_message": "",
    "environment_setup_s": 12.0,
    "agent_setup_s": 130.3,
    "agent_execution_s": 30.2,
    "verifier_s": 8.1,
    "n_input_tokens": 47582,
    "n_output_tokens": 1405,
    "n_cache_tokens": 0,
    "agent_metadata": {"mycode_reasoning_tokens": 308, "mycode_session_id": "5dc19fa4"},
    "result_path": "/home/han/.mycode/harbor_jobs/x/result.json",
}


@pytest.mark.parametrize(
    "benchmark,raw,task_id",
    [("gaia", TASK_RESULT, "gaia-1"), ("loca", LOCA_ROW, "64k-3"),
     ("harbor", HARBOR_TRIAL, "polyglot_cpp_yacht")],
)
def test_every_shape_lands_on_the_same_core(benchmark, raw, task_id):
    row = normalize_row(raw, benchmark=benchmark)

    assert set(row) == set(ROW_CORE)
    assert row["task_id"] == task_id
    assert set(row["tokens"]) == set(TOKEN_FIELDS)
    assert row["status"] in ("passed", "failed", "error", "unscored")


def test_correct_maps_onto_passed():
    # gaia/hle 只有 correct；判分口径必须统一，否则对比层要认两个字段
    assert normalize_row(TASK_RESULT, benchmark="gaia")["passed"] is True


def test_loca_reward_survives_but_gaia_has_none():
    assert normalize_row(LOCA_ROW, benchmark="loca")["reward"] == 0.75
    assert normalize_row(TASK_RESULT, benchmark="gaia")["reward"] is None


def test_harbor_reward_becomes_passed():
    assert normalize_row(HARBOR_TRIAL, benchmark="harbor")["passed"] is True
    assert normalize_row({**HARBOR_TRIAL, "reward": 0.0}, benchmark="harbor")["passed"] is False


def test_harbor_without_a_reward_is_unscored_not_failed():
    row = normalize_row({**HARBOR_TRIAL, "reward": None,
                         "exception_type": "NonZeroAgentExitCodeError",
                         "exception_message": "exit 1"}, benchmark="harbor")

    assert row["passed"] is None
    assert row["status"] == "error"
    assert "NonZeroAgentExitCodeError" in row["error"]


def test_a_scored_build_failure_is_failed_not_error():
    # cpp 构建不过：reward 记 0，同时带着 RewardFileNotFoundError。
    # 它是做错了，不能因为有个异常就被洗成"没跑起来"而不进分母
    row = normalize_row({**HARBOR_TRIAL, "reward": 0.0,
                         "exception_type": "RewardFileNotFoundError"}, benchmark="harbor")

    assert row["passed"] is False
    assert row["status"] == "failed"


def test_loca_error_prefers_the_agent_error():
    row = normalize_row({**LOCA_ROW, "agent_error": "boom", "passed": False}, benchmark="loca")

    assert row["error"] == "boom"
    assert row["status"] == "failed"  # 判过分了，就是失败，不是异常


def test_benchmark_specific_fields_all_land_in_extra():
    harbor = normalize_row(HARBOR_TRIAL, benchmark="harbor")
    loca = normalize_row(LOCA_ROW, benchmark="loca")
    gaia = normalize_row(TASK_RESULT, benchmark="gaia")

    assert harbor["extra"]["agent_setup_s"] == 130.3
    assert harbor["extra"]["no_reward_file"] is False
    assert harbor["extra"]["agent_metadata"]["mycode_reasoning_tokens"] == 308
    assert loca["extra"]["compression_events"]["tool_folded"] == 2
    assert loca["extra"]["step_info"] == {"steps": 12}
    assert gaia["extra"]["expected"] == "2019"
    assert gaia["extra"]["metadata"] == {"level": 3}


def test_harbor_reasoning_tokens_come_out_of_the_agent_metadata():
    tokens = normalize_row(HARBOR_TRIAL, benchmark="harbor")["tokens"]

    assert tokens == {"input": 47582, "output": 1405, "cached": 0, "reasoning": 308}


def test_unknown_token_counts_are_none_not_zero():
    tokens = normalize_row({**HARBOR_TRIAL, "n_input_tokens": None,
                            "n_output_tokens": None, "n_cache_tokens": None,
                            "agent_metadata": None}, benchmark="harbor")["tokens"]

    assert tokens == {"input": None, "output": None, "cached": None, "reasoning": None}


def test_an_existing_status_is_not_overwritten():
    row = normalize_row({**TASK_RESULT, "status": "aborted", "correct": None}, benchmark="gaia")

    assert row["status"] == "aborted" and row["passed"] is None


def test_reasoning_is_a_subset_of_output_not_an_extra_counter():
    # 网关的 completion_tokens **含**思考。把它当额外一份，"生成量"就会凭空多一倍——
    # harbor README §5.6 那次归因之所以绕了远路，就是因为这个口径没写下来
    row = normalize_row(TASK_RESULT, benchmark="gaia")

    assert row["tokens"]["reasoning"] == 40
    assert row["tokens"]["output"] == 300  # 不是 300 + 40
    assert summarize_rows([row])["totals"] == {
        "input_tokens": 1200, "output_tokens": 300,
        "reasoning_tokens": 40, "cached_tokens": 100,
    }


def test_summarize_rows_counts_unscored_separately():
    rows = [
        normalize_row(HARBOR_TRIAL, benchmark="harbor"),
        normalize_row({**HARBOR_TRIAL, "task_name": "b", "reward": 0.0}, benchmark="harbor"),
        normalize_row({**HARBOR_TRIAL, "task_name": "c", "reward": None}, benchmark="harbor"),
    ]

    summary = summarize_rows(rows)

    assert summary["n_tasks"] == 3
    assert summary["n_scored"] == 2 and summary["n_unscored"] == 1
    assert summary["n_passed"] == 1 and summary["pass_rate"] == 0.5
    assert summary["totals"] == {
        "input_tokens": 47582 * 3, "output_tokens": 1405 * 3,
        "reasoning_tokens": 308 * 3, "cached_tokens": 0,
    }


def test_summarize_rows_of_nothing_reports_unknown_not_zero():
    summary = summarize_rows([])

    assert summary["pass_rate"] is None and summary["mean_duration_s"] is None
    assert summary["n_tasks"] == 0


def test_normalize_report_fills_the_envelope_and_is_idempotent():
    once = normalize_report(
        {"benchmark": "gaia", "run_id": "gaia-x", "generated_at": "2026-10-18T00:00:00Z",
         "meta": {"model": "qwen3.6-27b", "seed": 42},
         "summary": {"total": 1, "correct": 1, "pass_at_1": 1.0},
         "results": [TASK_RESULT]}
    )

    assert set(once) == set(ENVELOPE_KEYS)
    assert check_report(once) == []
    # 旧键保留（前端还在读 total/pass_at_1），公共核心是加上去的
    assert once["summary"]["total"] == 1 and once["summary"]["n_passed"] == 1
    assert once["meta"]["task_ids"] == ["gaia-1"]
    assert once["meta"]["git_revision"] == "unknown" and once["meta"]["variables"] == {}
    assert normalize_report(once) == once


def test_check_report_names_every_missing_piece():
    problems = check_report({"benchmark": "gaia", "results": [{"task_id": "a"}]})

    assert any("run_id" in p for p in problems)
    assert any("meta" in p for p in problems)
    assert any("summary" in p for p in problems)


def test_check_report_complains_about_a_row_missing_core_fields():
    report = normalize_report(
        {"benchmark": "gaia", "run_id": "r", "generated_at": "t",
         "meta": {}, "summary": {}, "results": [TASK_RESULT]}
    )
    report["results"][0].pop("tokens")

    assert any("results[0]" in p and "tokens" in p for p in check_report(report))


def test_check_report_reports_at_most_one_problem_per_row():
    report = normalize_report(
        {"benchmark": "gaia", "run_id": "r", "generated_at": "t",
         "meta": {}, "summary": {}, "results": [TASK_RESULT] * 50}
    )
    for row in report["results"]:
        row.clear()

    assert len([p for p in check_report(report) if p.startswith("results[")]) == 50


def test_looks_like_run_report_rejects_compare_output_and_junk():
    assert looks_like_run_report(
        {"benchmark": "harbor", "run_id": "r", "results": []}
    )
    # harbor_compare_*.json 也在 eval/reports 里，形状像但不是运行记录
    assert not looks_like_run_report({"n_paired": 20, "mcnemar_p": 0.06, "pairs": []})
    assert not looks_like_run_report(None)
    assert not looks_like_run_report([1, 2, 3])
