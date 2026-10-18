"""对比层。重点是变量守卫——它是一次真事故换来的，必须有测试钉住。

事故：首轮 harbor 25 题得出"MyCode 快 1.86×"，而两臂的思考开关根本没对齐
（MyCode 发了 enable_thinking:false，opencode 没发）。对齐后差距缩到 1.17×。
假结论一路活到了汇报环节，因为报告里没有任何一处写下"这轮思考是关的"。
"""

from __future__ import annotations

import json

import pytest

from eval.common.compare import (
    VariableMismatch,
    _guard_was_blind,
    _label_of,
    compare_reports,
    compare_rows,
    headline,
    load_run,
    mcnemar_exact,
    pair_rows,
    render_markdown,
    rows_of,
    variable_mismatches,
)


def _row(task_id, passed, *, duration=10.0, inp=100, out=50, reasoning=0, error=None, **extra):
    return {
        "task_id": task_id, "name": task_id, "status": "passed" if passed else "failed",
        "passed": passed, "reward": 1.0 if passed else 0.0, "duration_s": duration,
        "tokens": {"input": inp, "output": out, "reasoning": reasoning, "cached": 0},
        "error": error, "session_id": None, "trace_id": None, "extra": extra,
    }


def _report(rows, *, run_id="r", benchmark="harbor", model="qwen3.6-27b", seed=42,
            git_revision="abc1234", **variables):
    return {
        "benchmark": benchmark, "run_id": run_id, "generated_at": "2026-10-18T00:00:00Z",
        "meta": {
            "model": model, "seed": seed, "git_revision": git_revision,
            "task_ids": [r["task_id"] for r in rows],
            "variables": {"model": model, "seed": seed, **variables},
        },
        "summary": {"n_tasks": len(rows), "n_passed": sum(1 for r in rows if r["passed"])},
        "results": rows,
    }


def _legacy_trial(task_name="polyglot_cpp_yacht", reward=1.0):
    """harbor 原始 trial 的词汇：task_name / n_*_tokens / agent_execution_s。"""
    return {
        "task_name": task_name, "reward": reward, "agent_execution_s": 30.2,
        "n_input_tokens": 47582, "n_output_tokens": 1405, "n_cache_tokens": 0,
        "exception_type": None,
    }


def _legacy_report(trials, *, variables="__absent__", arm="mycode"):
    """盘上真 harbor 报告的形状（`harbor_*_final_2026101*.json`）。

    没有 benchmark / run_id / meta / results，控制变量在 **`summary.variables`**。
    `write_report` 一直是 `summary["variables"] = dict(variables or {})`
    （`eval/harbor/runner.py:845`），Task 8 之后才两处都有。
    """
    summary = {"arm": arm, "n_trials": len(trials)}
    if variables != "__absent__":
        summary["variables"] = variables
    return {"summary": summary, "trials": trials}


ALIGNED = {"thinking_mode": "default", "context_window": 1000000}


# ── McNemar：从 harbor/compare.py 原样搬过来，这四条是它的既有测试 ──


def test_mcnemar_of_no_discordance_is_one():
    assert mcnemar_exact(0, 0) == 1.0


def test_mcnemar_matches_the_reference_value():
    assert mcnemar_exact(8, 2) == pytest.approx(0.109375)


def test_mcnemar_is_symmetric():
    assert mcnemar_exact(8, 2) == mcnemar_exact(2, 8)


def test_mcnemar_never_exceeds_one():
    assert mcnemar_exact(1, 1) == 1.0


def test_mcnemar_gets_small_when_discordance_is_one_sided():
    # 25 题的可检测下限就是从这里来的：5:0 → 0.0625，6:0 → 0.031
    assert mcnemar_exact(5, 0) == pytest.approx(0.0625)
    assert mcnemar_exact(6, 0) == pytest.approx(0.03125)
    assert mcnemar_exact(10, 0) == pytest.approx(2 / 2**10)
    assert mcnemar_exact(10, 0) < mcnemar_exact(6, 4)


# ── 配对 ──


def test_pair_rows_matches_by_task_id_and_keeps_single_side_tasks():
    pairs = pair_rows([_row("a", True), _row("b", False)], [_row("a", False), _row("c", True)])

    assert [p["task_id"] for p in pairs] == ["a", "b", "c"]
    assert pairs[1]["b"] is None and pairs[1]["b_passed"] is None
    assert pairs[2]["a"] is None


def test_pair_rows_keeps_the_latest_of_a_repeated_task():
    pairs = pair_rows([_row("t", False), _row("t", True)], [])

    assert len(pairs) == 1 and pairs[0]["a_passed"] is True


def test_pair_rows_only_pairs_scored_tasks():
    result = compare_rows(
        [_row("a", True), _row("b", False), _row("c", None)],
        [_row("a", False), _row("b", False), _row("c", True)],
    )

    assert result["n_paired"] == 2
    assert result["n_unscored"] == 1 and result["unscored_tasks"] == ["c"]


def test_pair_rows_names_the_discordant_tasks():
    result = compare_rows(
        [_row("win", True), _row("lose", False)],
        [_row("win", False), _row("lose", True)],
    )

    assert result["only_a"] == ["win"] and result["only_b"] == ["lose"]
    assert result["mcnemar_p"] == 1.0


def test_a_one_sided_sweep_is_the_strongest_thing_this_n_can_say():
    result = compare_rows(
        [_row(f"t{i}", True) for i in range(5)] + [_row("both", True)],
        [_row(f"t{i}", False) for i in range(5)] + [_row("both", True)],
    )

    assert result["only_b"] == [] and result["mcnemar_p"] == pytest.approx(0.0625)


def test_compare_of_nothing_reports_unknown_not_zero():
    result = compare_rows([], [])

    assert result["n_paired"] == 0
    assert result["a_pass_rate"] is None and result["b_pass_rate"] is None
    assert result["means"]["duration_s"] == {"a": None, "b": None}


def test_means_are_taken_over_scored_pairs_only():
    result = compare_rows(
        [_row("a", True, duration=10.0, inp=100), _row("skip", None, duration=999.0, inp=999)],
        [_row("a", False, duration=20.0, inp=200), _row("skip", None, duration=1.0, inp=1)],
    )

    assert result["means"]["duration_s"] == {"a": 10.0, "b": 20.0}
    assert result["means"]["tokens.input"] == {"a": 100.0, "b": 200.0}


def test_metrics_reach_into_nested_paths():
    result = compare_rows([_row("a", True, reasoning=308)], [_row("a", True, reasoning=0)])

    assert result["means"]["tokens.reasoning"] == {"a": 308.0, "b": 0.0}


def test_a_metric_that_is_none_on_both_sides_stays_none():
    rows = [{**_row("a", True), "tokens": {"input": None, "output": None,
                                           "reasoning": None, "cached": None}}]
    result = compare_rows(rows, rows)

    assert result["means"]["tokens.input"] == {"a": None, "b": None}


def test_a_zero_duration_does_not_drag_the_mean_down():
    """`duration_s == 0.0` 是 schema 对"未知耗时"的兜底，不是"跑了 0 秒"。

    `normalize_row` 把非数值耗时兜成 0.0，而 harbor 那种"agent 没起来"的 trial
    正是这个形状。没跑起来的题恰恰更容易落在弱的那一臂，
    让它以 0 秒进均值等于给弱臂白送一个好看的平均耗时。
    `summarize_rows` 早就用 `duration_s > 0` 当"真有时长"，对比层必须同口径。
    """
    result = compare_rows(
        [_row("fast", True, duration=10.0), _row("never-ran", True, duration=0.0)],
        [_row("fast", True, duration=10.0), _row("never-ran", True, duration=30.0)],
    )

    # a 侧只有 10.0 进均值（0.0 被当成未知剔除），不是 (10+0)/2 = 5.0
    assert result["means"]["duration_s"] == {"a": 10.0, "b": 20.0}


def test_a_zero_token_count_is_a_real_measurement_not_unknown():
    """和上一条对照：token 的 0 是**真值**，不能一起被剔掉。

    opencode 臂的 `reasoning` 恒为 0（它不报告思考量），把 0 当未知就会
    让两臂的思考量都变成 None，看不出差别——而那正是要比的东西。
    tokens 的"未知"在 schema 里是 `None`（口径裁定 3），和 duration 的 0.0 不是一套。
    """
    result = compare_rows([_row("a", True, reasoning=0)], [_row("a", True, reasoning=308)])

    assert result["means"]["tokens.reasoning"] == {"a": 0.0, "b": 308.0}


def test_dimension_splits_the_table():
    def language(task_id, row):
        return task_id.split("_")[1] if task_id.startswith("polyglot_") else None

    result = compare_rows(
        [_row("polyglot_cpp_a", True), _row("polyglot_go_b", False)],
        [_row("polyglot_cpp_a", False), _row("polyglot_go_b", False)],
        dimension=language,
    )

    assert result["by_dimension"] == {"cpp": {"n": 1, "a": 1, "b": 0},
                                      "go": {"n": 1, "a": 0, "b": 0}}


def test_exclude_drops_a_whole_dimension():
    def language(task_id, row):
        return task_id.split("_")[1]

    result = compare_rows(
        [_row("polyglot_java_a", False), _row("polyglot_rust_b", True)],
        [_row("polyglot_java_a", False), _row("polyglot_rust_b", False)],
        dimension=language, exclude=("java",),
    )

    # java 那题两臂都因环境恒 0，留着只会稀释掉真正测到的部分
    assert result["excluded"] == ["java"]
    assert result["n_paired"] == 1
    assert [p["task_id"] for p in result["pairs"]] == ["polyglot_rust_b"]


def test_excluding_something_absent_is_a_noop():
    result = compare_rows([_row("a", True)], [_row("a", True)], exclude=("java",))

    assert result["excluded"] == [] and result["n_paired"] == 1


# ── 变量守卫 ──


def test_identical_runs_have_no_mismatch():
    a = _report([_row("t", True)], run_id="a", **ALIGNED)

    assert variable_mismatches(a, _report([_row("t", True)], run_id="b", **ALIGNED)) == []


def test_a_different_model_is_a_mismatch():
    found = variable_mismatches(
        _report([_row("t", True)], model="qwen3.6-27b", **ALIGNED),
        _report([_row("t", True)], model="deepseek-v4.1-flash", **ALIGNED),
    )

    assert VariableMismatch("meta.model", "qwen3.6-27b", "deepseek-v4.1-flash") in found


def test_a_different_seed_is_a_mismatch():
    found = variable_mismatches(
        _report([_row("t", True)], seed=42, **ALIGNED),
        _report([_row("t", True)], seed=7, **ALIGNED),
    )

    assert any(m.field == "meta.seed" for m in found)


def test_a_different_git_revision_is_a_mismatch():
    # 回归基线对比里，commit 不同是**预期的**；但守卫必须说出来，
    # 由渲染层决定怎么措辞，而不是悄悄当成同一份代码
    found = variable_mismatches(
        _report([_row("t", True)], git_revision="abc1234", **ALIGNED),
        _report([_row("t", True)], git_revision="def5678", **ALIGNED),
    )

    assert any(m.field == "meta.git_revision" for m in found)


def test_a_different_benchmark_is_a_mismatch():
    found = variable_mismatches(
        _report([_row("t", True)], benchmark="harbor", **ALIGNED),
        _report([_row("t", True)], benchmark="gaia", **ALIGNED),
    )

    assert any(m.field == "benchmark" for m in found)


def test_every_variable_key_is_checked_even_if_one_side_lacks_it():
    # 旧报告没有 variables；"一边有一边没有"必须报出来，
    # 否则拿旧报告当基线就等于默认它所有变量都对齐
    found = variable_mismatches(
        _report([_row("t", True)], thinking_mode="default"),
        _report([_row("t", True)]),
    )

    assert VariableMismatch("variables.thinking_mode", "default", None) in found


def test_variables_under_summary_are_seen_too():
    """旧 harbor 报告把控制变量写在 `summary.variables`，不是 `meta.variables`。

    只读 `meta` 的话这两侧都变成空字典，守卫于是什么都不报——而"什么都没校验"
    和"校验通过"在输出上完全一样。这正是 1.86× 那次假结论能活下来的机制。
    """
    a = _legacy_report([_legacy_trial()], variables={"thinking_mode": "off"}, arm="mycode")
    b = _legacy_report([_legacy_trial()], variables={"thinking_mode": "default"}, arm="opencode")

    found = variable_mismatches(a, b)

    assert VariableMismatch("variables.thinking_mode", "off", "default") in found


def test_legacy_variables_are_the_only_source_of_model_too():
    """旧报告连 `meta` 都没有，model 只存在于 `summary.variables` 里。

    守卫若只读 `meta.model`，这两侧都是 None，模型换了也报不出来。
    """
    a = _legacy_report([_legacy_trial()], variables={"model": "qwen3.6-27b"})
    b = _legacy_report([_legacy_trial()], variables={"model": "deepseek-v4.1-flash"})

    found = variable_mismatches(a, b)

    assert VariableMismatch("meta.model", "qwen3.6-27b", "deepseek-v4.1-flash") in found


def test_legacy_reports_without_any_variables_are_flagged_as_unverified():
    """两份都没记录变量的旧报告 = 守卫没东西可查，不能沉默当成"对齐"。

    这就是 1.86× 那次的输入形状：`harbor_mycode_final_20261017__232016.json` 和
    `harbor_opencode_final_20261017__232016.json` 两份都没有 `summary.variables`。
    """
    a = _legacy_report([_legacy_trial("t", 1.0)], arm="mycode")
    b = _legacy_report([_legacy_trial("t", 0.0)], arm="opencode")
    result = compare_reports(a, b)

    assert result["variable_mismatches"] == []  # 空列表在这里是坏消息，不是好消息
    assert result["variables_seen"] == {"a": 0, "b": 0}
    assert "没有记录控制变量" in headline(result)
    for word in SUPERLATIVES:
        assert word not in headline(result), f"没做变量校验却出现了结论性措辞：{word}"
    for phrase in WINNER_PHRASES:
        assert phrase not in headline(result), f"没做变量校验却出现了胜者句式：{phrase}"


def test_task_id_sets_are_compared_not_just_their_length():
    found = variable_mismatches(
        _report([_row("a", True), _row("b", True)], **ALIGNED),
        _report([_row("a", True), _row("c", True)], **ALIGNED),
    )

    assert any(m.field == "meta.task_ids" for m in found)


def test_compare_reports_attaches_the_mismatches():
    result = compare_reports(
        _report([_row("t", True)], run_id="a", model="m1", **ALIGNED),
        _report([_row("t", False)], run_id="b", model="m2", **ALIGNED),
    )

    assert [m["field"] for m in result["variable_mismatches"]] == ["meta.model"]
    assert result["a_report"] == "a" and result["b_report"] == "b"


# ── 措辞：这是守卫的**执行**环节，光检测出来不拦住等于没做 ──

SUPERLATIVES = ("更强", "更好", "更差", "胜出", "优于", "领先", "落后", "打赢", "击败")

# 胜者句式，和 SUPERLATIVES 是两回事，必须单独禁。
# "多过 N 题" 就是本文件认定的胜者句（见 test_an_aligned_comparison_may_state_a_winner），
# 却不在 SUPERLATIVES 里。只查 SUPERLATIVES 会漏掉它：把 headline 的未对齐分支改成
# "变量未对齐（1 项不同）：… 。mycode 多过 6 题。" 之后，全套 40 条**一条都不红**
# ——变量没对齐的对比照样说出了谁赢，而这正是本模块存在的唯一理由。
WINNER_PHRASES = ("多过",)


def test_an_aligned_comparison_may_state_a_winner():
    result = compare_reports(
        _report([_row(f"t{i}", True) for i in range(6)], run_id="a", **ALIGNED),
        _report([_row(f"t{i}", False) for i in range(6)], run_id="b", **ALIGNED),
    )

    assert "多过 6 题" in headline(result)


def test_a_variable_mismatch_removes_every_superlative():
    result = compare_reports(
        _report([_row(f"t{i}", True) for i in range(6)], run_id="a", model="m1", **ALIGNED),
        _report([_row(f"t{i}", False) for i in range(6)], run_id="b", model="m2", **ALIGNED),
    )
    text = render_markdown(result)

    assert "变量未对齐" in text
    assert "meta.model" in text and "m1" in text and "m2" in text
    for word in SUPERLATIVES:
        assert word not in text, f"变量没对齐却出现了结论性措辞：{word}"
    for phrase in WINNER_PHRASES:
        assert phrase not in text, f"变量没对齐却出现了胜者句式：{phrase}"
    # 描述性数字仍要给全，否则这一页就没用了
    assert "6" in text and str(result["mcnemar_p"]) in text


def test_an_aligned_comparison_renders_the_mcnemar_p_and_the_per_task_table():
    result = compare_reports(
        _report([_row("a", True), _row("b", False)], run_id="x", **ALIGNED),
        _report([_row("a", False), _row("b", False)], run_id="y", **ALIGNED),
    )
    text = render_markdown(result)

    assert "McNemar" in text and "| a |" in text and "| b |" in text


def test_a_tie_says_no_difference_was_detected():
    result = compare_reports(
        _report([_row("a", True), _row("b", False)], run_id="x", **ALIGNED),
        _report([_row("a", False), _row("b", True)], run_id="y", **ALIGNED),
    )

    assert result["mcnemar_p"] == 1.0
    assert "没测出" in headline(result)


def test_a_small_p_is_still_bounded_by_the_pair_count():
    # p 小不等于结论硬：n_paired 必须和 p 一起出现，否则 5:0 会被读成"显著更强"
    result = compare_reports(
        _report([_row(f"t{i}", True) for i in range(5)], run_id="a", **ALIGNED),
        _report([_row(f"t{i}", False) for i in range(5)], run_id="b", **ALIGNED),
    )
    text = render_markdown(result)

    assert result["mcnemar_p"] == pytest.approx(0.0625)
    assert "配对题数：5" in text


def test_headline_says_when_the_guard_had_nothing_to_check():
    """守卫沉默 = 没有守卫。三种情形必须在措辞里分得开。

    1.86× 那次事故里两份报告都没记下"思考是关的"，对比却照样给了结论。
    两侧变量都为空时 `variable_mismatches` **也是空的**——"什么都没校验"和
    "校验通过"在输出上必须能区分，否则守卫恒绿，比没有守卫更坏。
    """
    # 1) 两侧都没记录任何控制变量 → 显式警告，且不给结论性措辞
    blind_a = _report([_row(f"t{i}", True) for i in range(6)], run_id="a")
    blind_b = _report([_row(f"t{i}", False) for i in range(6)], run_id="b")
    blind_a["meta"]["variables"] = {}
    blind_b["meta"]["variables"] = {}
    blind = compare_reports(blind_a, blind_b)
    text = headline(blind)

    assert blind["variable_mismatches"] == []  # 空列表在这里恰恰是坏消息
    assert "没有记录控制变量" in text
    for word in SUPERLATIVES:
        assert word not in text, f"没做变量校验却出现了结论性措辞：{word}"
    for phrase in WINNER_PHRASES:
        assert phrase not in text, f"没做变量校验却出现了胜者句式：{phrase}"
    assert "没有记录控制变量" in render_markdown(blind)

    # 2) 有变量且对齐 → 无警告，照常给结论
    aligned = compare_reports(
        _report([_row("t", True)], run_id="a", **ALIGNED),
        _report([_row("t", True)], run_id="b", **ALIGNED),
    )

    assert aligned["variable_mismatches"] == []
    assert "没有记录控制变量" not in headline(aligned)
    assert "变量已对齐" in headline(aligned)

    # 3) 有变量但不对齐 → 列出差异，不冒充"没记录"
    mismatched = compare_reports(
        _report([_row("t", True)], run_id="a", model="m1", **ALIGNED),
        _report([_row("t", True)], run_id="b", model="m2", **ALIGNED),
    )
    text = headline(mismatched)

    assert "变量未对齐" in text
    assert "没有记录控制变量" not in text
    # 未对齐时连胜者句式都不许有——只禁 SUPERLATIVES 会漏掉"多过 N 题"
    for phrase in WINNER_PHRASES:
        assert phrase not in text, f"变量没对齐却出现了胜者句式：{phrase}"


# ── 读报告 ──


def test_rows_of_reads_the_unified_results(tmp_path):
    report = _report([_row("a", True)])

    assert [r["task_id"] for r in rows_of(report)] == ["a"]


def test_rows_of_falls_back_to_harbor_trials_on_disk(tmp_path):
    # 2026-10-18 之前落盘的 harbor 报告用的是 trials；重跑一次要 45 分钟网关时间，
    # 而 README §5 的结论就引自它们，所以旧报告必须还能读
    path = tmp_path / "harbor_mycode_final_20261017__232016.json"
    path.write_text(json.dumps({
        "summary": {"arm": "mycode"},
        "trials": [{"task_name": "polyglot_cpp_yacht", "reward": 1.0,
                    "agent_execution_s": 30.2, "n_input_tokens": 47582,
                    "n_output_tokens": 1405, "n_cache_tokens": 0,
                    "exception_type": None}],
    }), encoding="utf-8")

    rows = rows_of(json.loads(path.read_text(encoding="utf-8")))

    assert rows[0]["task_id"] == "polyglot_cpp_yacht"
    assert rows[0]["passed"] is True
    assert rows[0]["tokens"]["input"] == 47582


def test_load_run_accepts_a_path_or_a_run_id(tmp_path):
    report = _report([_row("a", True)], run_id="harbor_mycode_final_20261018__025351")
    (tmp_path / "harbor_mycode_final_20261018__025351.json").write_text(
        json.dumps(report), encoding="utf-8"
    )

    assert load_run(tmp_path / "harbor_mycode_final_20261018__025351.json")["run_id"] == report["run_id"]
    assert load_run("harbor_mycode_final_20261018__025351", reports_dir=tmp_path)["benchmark"] == "harbor"


def test_load_run_of_something_missing_says_what_it_looked_for(tmp_path):
    with pytest.raises(FileNotFoundError) as exc:
        load_run("nope", reports_dir=tmp_path)

    assert "nope" in str(exc.value) and str(tmp_path) in str(exc.value)


# ── 修复轮 1：`compare_rows` 不许绕过守卫；守卫措辞钉成金标准串 ──
#
# 评审查出的 Critical：`compare_rows` 的结果喂 `headline()` 会在**零变量校验**下
# 照样报出胜者——实测输出就是
# `mycode 多过 6 题（mycode 6/6 vs opencode 0/6，McNemar p = 0.0312）`。
# 而 Task 12 要 import 的正是 `compare_rows`/`pair_rows`/`rows_of`，
# 所以这条公开路径必须自己守住，不能指望调用方先去过 `compare_reports`。


def test_compare_rows_alone_may_not_claim_a_winner():
    """Critical 本体：没做过变量校验的对比不许下胜负结论。"""
    result = compare_rows(
        [_row(f"t{i}", True) for i in range(6)],
        [_row(f"t{i}", False) for i in range(6)],
        labels=("mycode", "opencode"),
    )

    assert "variables_seen" not in result  # 这一层确实看不到报告，所以无从校验
    text = headline(result)
    assert "未经过变量校验" in text
    for phrase in WINNER_PHRASES:
        assert phrase not in text, f"零校验却出现了胜者句式：{phrase}"
    for word in SUPERLATIVES:
        assert word not in text, f"零校验却出现了结论性措辞：{word}"
    # 描述性数字仍要给全：守卫拦的是结论，不是数据
    assert "mycode 6/6 vs opencode 0/6" in text and "0.0312" in text

    md = render_markdown(result)
    assert "未经过变量校验" in md
    for phrase in WINNER_PHRASES:
        assert phrase not in md, f"零校验的 markdown 里出现了胜者句式：{phrase}"


def test_one_side_without_variables_is_blind_too():
    """钉 `all` 而不是 `any`：一侧没记录变量就等于没法比对。

    评审构造的形状是 `variables_seen == {'a': 0, 'b': 1}`；`any` 会判它"不瞎"，
    于是 b 侧记了一个变量就足以让 a 侧的空白被当成对齐——1.86× 那次事故
    正是"没人记下思考是关的"，一侧记了另一侧没记同样得不出结论。
    """
    a = _report([_row("t", True)], run_id="mycode")
    a["meta"]["variables"] = {}  # a 侧什么都没记
    b = _report([_row("t", False)], run_id="opencode")  # meta.variables 里有 model/seed

    result = compare_reports(a, b)

    assert result["variables_seen"] == {"a": 0, "b": 2}
    assert result["variable_mismatches"] == []  # model/seed 按有效值比过，两边一致
    text = headline(result)
    assert "未经过变量校验" in text and "没有记录控制变量" in text
    for phrase in WINNER_PHRASES:
        assert phrase not in text, f"一侧零变量却出现了胜者句式：{phrase}"


def test_a_comparison_without_variables_seen_is_blind_not_aligned():
    """钉 `isinstance` 那半边：键**完全缺失** = 没校验，不是"这一层没报告可比"。"""
    bare = {
        "labels": {"a": "mycode", "b": "opencode"},
        "n_paired": 1, "mcnemar_p": 1.0,
        "a_passed": 1, "b_passed": 0,
        "only_a": ["t"], "only_b": [],
        "variable_mismatches": [],
    }

    assert "variables_seen" not in bare
    text = headline(bare)

    assert "未经过变量校验" in text
    for phrase in WINNER_PHRASES:
        assert phrase not in text, f"没有 variables_seen 却出现了胜者句式：{phrase}"


def test_the_unaligned_headline_is_a_golden_string():
    """守卫分支之一：逐字符钉死。

    禁词表永远列不全（评审实测 `快 1.86×` 与 `赢下 6 题` 都能穿过去），
    精确串则是多一个字、少一个字都红，不需要预先想到那个词。
    测试与生产各存一份是**特性**：改措辞就必须有意识地改这里。
    """
    result = compare_reports(
        _report([_row(f"t{i}", True) for i in range(6)], run_id="mycode", model="m1", **ALIGNED),
        _report([_row(f"t{i}", False) for i in range(6)], run_id="opencode", model="m2", **ALIGNED),
    )

    assert [m["field"] for m in result["variable_mismatches"]] == ["meta.model"]
    assert headline(result) == (
        "变量未对齐（1 项不同）：mycode 6/6 vs opencode 0/6，McNemar p = 0.0312。"
        "下面只是描述性数字，不构成结论。"
    )


def test_the_blind_headline_is_a_golden_string():
    """守卫分支之二：同样逐字符钉死。"""
    a = _report([_row(f"t{i}", True) for i in range(6)], run_id="mycode", **ALIGNED)
    b = _report([_row(f"t{i}", False) for i in range(6)], run_id="opencode", **ALIGNED)
    a["meta"]["variables"] = {}
    b["meta"]["variables"] = {}
    result = compare_reports(a, b)

    assert result["variable_mismatches"] == [] and result["variables_seen"] == {"a": 0, "b": 0}
    assert headline(result) == (
        "⚠️ 本次对比未经过变量校验（mycode 与 opencode 都没有记录控制变量）："
        "mycode 6/6 vs opencode 0/6，McNemar p = 0.0312。"
        "下面只是描述性数字，不构成结论。"
    )


def test_legacy_reports_label_themselves_by_arm_not_by_the_string_none():
    """盘上所有旧 harbor 报告都没有 `run_id`，`str(None)` 曾把标题渲染成 `# None vs None`。

    注：这里只钉**标签位置**不许出现 "None"，不钉整篇 markdown——均值表把
    "未知指标"渲染成字面量 `None`（旧 trial 没有 reasoning token 字段）是
    `render_markdown` 的另一处毛病，本轮按简报裁定不动横幅以外的部分，留终审。
    """
    a = _legacy_report([_legacy_trial("t", 1.0)], arm="mycode")
    b = _legacy_report([_legacy_trial("t", 0.0)], arm="opencode")

    result = compare_reports(a, b)
    md = render_markdown(result)
    head = headline(result)

    assert result["labels"] == {"a": "mycode", "b": "opencode"}
    assert md.splitlines()[0] == "# mycode vs opencode"
    assert "None" not in md.splitlines()[0]
    assert "mycode 1/1 vs opencode 0/1" in head
    assert "None" not in head


def test_explicit_labels_reach_both_renders_verbatim():
    """`labels=` 这条分支评审确认**零覆盖**，而 Task 13 的路由走的正是它。"""
    result = compare_reports(
        _report([_row("t", True)], run_id="ignored_a", **ALIGNED),
        _report([_row("t", False)], run_id="ignored_b", **ALIGNED),
        labels=("MyCode", "opencode"),
    )
    md = render_markdown(result)

    assert md.splitlines()[0] == "# MyCode vs opencode"
    assert "MyCode 1/1 vs opencode 0/1" in headline(result)
    assert "ignored_a" not in md and "ignored_b" not in md


def test_a_report_with_no_identity_at_all_falls_back_to_a_question_mark():
    """退化报告：连 arm 和 benchmark 都没有，也不能退成字符串 "None"。"""
    result = compare_reports({"results": [_row("t", True)]}, {"results": [_row("t", False)]})
    md = render_markdown(result)

    assert result["labels"] == {"a": "?", "b": "?"}
    assert md.splitlines()[0] == "# ? vs ?"
    assert "None" not in md


def test_the_guard_banners_are_above_the_fold():
    """横幅是"变量没对齐"在人眼里的唯一提示，所以只断言"某处包含"不够。

    评审实测：把 ⚠️ 横幅整段删掉，40 条测试**一条都不红**。
    这里钉住横幅的精确文字 + 它必须出现在第一屏、且在均值表之前。
    """
    mismatched = compare_reports(
        _report([_row(f"t{i}", True) for i in range(6)], run_id="mycode", model="m1", **ALIGNED),
        _report([_row(f"t{i}", False) for i in range(6)], run_id="opencode", model="m2", **ALIGNED),
    )
    lines = render_markdown(mismatched).splitlines()
    banners = [i for i, ln in enumerate(lines) if ln.startswith(">") and "变量未对齐" in ln]

    assert len(banners) == 1, f"横幅该有且只有一条，实际在 {banners}"
    assert lines[banners[0]] == "> ⚠️ **变量未对齐**——下面所有数字都是描述性的，不构成结论。"
    assert lines[banners[0] + 1] == "> 对齐之后重跑，或者只把它当线索。"
    assert banners[0] <= 3, f"横幅掉到第 {banners[0] + 1} 行，第一屏看不见"
    means = next(i for i, ln in enumerate(lines) if ln.startswith("| 指标 |"))
    assert banners[0] < means, "横幅必须在均值表之前，否则读者先看到的是数字"

    blind_a = _report([_row("t", True)], run_id="mycode", **ALIGNED)
    blind_b = _report([_row("t", False)], run_id="opencode", **ALIGNED)
    blind_a["meta"]["variables"] = {}
    blind_b["meta"]["variables"] = {}
    blind_lines = render_markdown(compare_reports(blind_a, blind_b)).splitlines()
    blind_banners = [i for i, ln in enumerate(blind_lines) if ln.startswith(">") and "未经过变量校验" in ln]

    assert len(blind_banners) == 1
    assert blind_lines[blind_banners[0]] == (
        "> ⚠️ 本次对比未经过变量校验（mycode 与 opencode 都没有记录控制变量）。"
    )
    assert blind_lines[blind_banners[0] + 1] == "> 下面所有数字都是描述性的，不构成结论。"
    assert blind_banners[0] <= 3


def test_guard_was_blind_treats_missing_and_one_sided_zero_as_blind():
    """把 `_guard_was_blind` 的输入逐个钉住——它是守卫的判据本身。

    `any` 与 `all` 这两种语义原有测试**分辨不出来**（评审实测把 `any` 改成 `all`
    也是 40 passed），所以这里直接钉判据，否则下一个人改回去没人知道。
    """
    assert _guard_was_blind({}) is True                        # 键缺失 = 没校验过
    assert _guard_was_blind({"variables_seen": None}) is True   # 不是 Mapping 同样算缺失
    assert _guard_was_blind({"variables_seen": {"a": 0, "b": 1}}) is True  # 一侧没记 = 没法比对
    assert _guard_was_blind({"variables_seen": {"a": 1, "b": 0}}) is True
    assert _guard_was_blind({"variables_seen": {"a": 0, "b": 0}}) is True  # 1.86× 那次的形状
    assert _guard_was_blind({"variables_seen": {"a": 2, "b": 4}}) is False  # 真校验过


def test_the_label_fallback_chain_is_ordered_and_never_yields_the_string_none():
    """`str(None)` 的坑：回退判断必须在 `str()` **之前**做，否则第一层永远"取到了"。"""
    full = {"run_id": "rid", "meta": {"arm": "m"}, "summary": {"arm": "s"}, "benchmark": "gaia"}

    assert _label_of(full) == "rid"
    assert _label_of({k: v for k, v in full.items() if k != "run_id"}) == "m"
    assert _label_of({"summary": {"arm": "s"}, "benchmark": "gaia"}) == "s"
    assert _label_of({"benchmark": "gaia"}) == "gaia"
    assert _label_of({}) == "?"
    # 每一层都要防"键在、值是 None"和"值是空白串"
    assert _label_of({"run_id": None, "meta": None, "summary": None, "benchmark": None}) == "?"
    assert _label_of({"run_id": "   ", "benchmark": "gaia"}) == "gaia"
    assert _label_of({}, fallback="x") == "x"
