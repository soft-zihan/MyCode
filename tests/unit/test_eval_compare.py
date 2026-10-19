"""对比层。重点是变量守卫——它是一次真事故换来的，必须有测试钉住。

事故：首轮 harbor 25 题得出"MyCode 快 1.86×"，而两臂的思考开关根本没对齐
（MyCode 发了 enable_thinking:false，opencode 没发）。对齐后差距缩到 1.17×。
假结论一路活到了汇报环节，因为报告里没有任何一处写下"这轮思考是关的"。
"""

from __future__ import annotations

import json

import pytest

from eval.common.compare import (
    GUARD_BLIND_WARNING,
    VariableMismatch,
    _guard_blind_reason,
    _guard_was_blind,
    _label_of,
    compare_reports,
    compare_rows,
    guard_banner,
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
    """Critical 本体：没做过变量校验的对比不许下胜负结论。

    修复轮 2（N1）把这里的子串断言升级成**全串精确相等**。原来只断言
    `"未经过变量校验" in text`，而守卫那句话的原因串走的是 `_guard_blind_reason`
    的第 1 支（`compare_rows` 层）——复审重造的 M8 变异往那一支后面追加
    `。mycode 快 1.86×`，51 条测试**全绿存活**，headline 与 markdown 横幅双双被污染。
    夹带在一句"不构成结论"的警告中间，人读起来反而更可信。
    """
    result = compare_rows(
        [_row(f"t{i}", True) for i in range(6)],
        [_row(f"t{i}", False) for i in range(6)],
        labels=("mycode", "opencode"),
    )

    assert "variables_seen" not in result  # 这一层确实看不到报告，所以无从校验
    text = headline(result)
    assert text == (
        "⚠️ 本次对比未经过变量校验"
        "（这一层只看得到逐题行、看不到报告，无从校验控制变量）："
        "mycode 6/6 vs opencode 0/6，McNemar p = 0.0312。"
        "下面只是描述性数字，不构成结论。"
    )
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
    # 横幅同样钉成精确串：它是人眼里的唯一提示，M8 就是从这里穿过去的
    md_lines = md.splitlines()
    banner = [i for i, ln in enumerate(md_lines) if ln.startswith(">") and "未经过变量校验" in ln]
    assert len(banner) == 1, f"守卫横幅该有且只有一条，实际在 {banner}"
    assert md_lines[banner[0]] == (
        "> ⚠️ 本次对比未经过变量校验（这一层只看得到逐题行、看不到报告，无从校验控制变量）。"
    )
    assert md_lines[banner[0] + 1] == "> 下面所有数字都是描述性的，不构成结论。"
    assert banner[0] <= 3, f"横幅掉到第 {banner[0] + 1} 行，第一屏看不见"


def test_one_side_without_variables_is_blind_too():
    """钉 `all` 而不是 `any`：一侧没记录变量就等于没法比对。

    评审构造的形状是 `variables_seen == {'a': 0, 'b': 1}`；`any` 会判它"不瞎"，
    于是 b 侧记了一个变量就足以让 a 侧的空白被当成对齐——1.86× 那次事故
    正是"没人记下思考是关的"，一侧记了另一侧没记同样得不出结论。

    修复轮 2（N1）把子串断言升级成**全串精确相等**：这一条钉的是
    `_guard_blind_reason` 的第 3 支（一侧…），复审重造的 M7 变异往那一支后面
    追加 `。mycode 快 1.86×`，51 条测试**全绿存活**。而 `{'a': 0, 'b': 2}`
    正是 1.86× 事故自己的形状。
    """
    a = _report([_row("t", True)], run_id="mycode")
    a["meta"]["variables"] = {}  # a 侧什么都没记
    b = _report([_row("t", False)], run_id="opencode")  # meta.variables 里有 model/seed

    result = compare_reports(a, b)

    assert result["variables_seen"] == {"a": 0, "b": 2}
    assert result["variable_mismatches"] == []  # model/seed 按有效值比过，两边一致
    text = headline(result)
    assert text == (
        "⚠️ 本次对比未经过变量校验（mycode 一侧没有记录控制变量）："
        "mycode 1/1 vs opencode 0/1，McNemar p = 1.0。"
        "下面只是描述性数字，不构成结论。"
    )
    for phrase in WINNER_PHRASES:
        assert phrase not in text, f"一侧零变量却出现了胜者句式：{phrase}"

    md_lines = render_markdown(result).splitlines()
    banner = [i for i, ln in enumerate(md_lines) if ln.startswith(">") and "未经过变量校验" in ln]
    assert len(banner) == 1, f"守卫横幅该有且只有一条，实际在 {banner}"
    assert md_lines[banner[0]] == "> ⚠️ 本次对比未经过变量校验（mycode 一侧没有记录控制变量）。"
    assert md_lines[banner[0] + 1] == "> 下面所有数字都是描述性的，不构成结论。"
    assert banner[0] <= 3, f"横幅掉到第 {banner[0] + 1} 行，第一屏看不见"


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
    """退化报告：连 arm 和 benchmark 都没有，也不能退成字符串 "None"。

    修复轮 2（N5）之后两侧标签还必须是**可区分**的：`# ? vs ?` 连"是同一臂自己
    比自己"还是"两臂都认不出来"都分不开，所以自动推导出的同名标签会加后缀。
    这是**加强**而不是放宽——原来两个标签相同，现在两个标签不同。
    `_label_of({})` 本身仍然返回 `"?"`（四层回退链一个字没动，
    由 `test_the_label_fallback_chain_is_ordered_and_never_yields_the_string_none` 看守）。
    """
    result = compare_reports({"results": [_row("t", True)]}, {"results": [_row("t", False)]})
    md = render_markdown(result)

    assert result["labels"] == {"a": "? (a)", "b": "? (b)"}
    assert md.splitlines()[0] == "# ? (a) vs ? (b)"
    assert "None" not in md
    assert _label_of({}) == "?"  # 去重加在 compare_reports 上，不在回退链里


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

    修复轮 2（N2）：判据从**键无关**的 `all(seen.values())` 改成**看 a/b 两个键**。
    `variables_seen` 的口径是"每一侧看到几个控制变量"（`compare_reports` 写的就是
    `{"a": int, "b": int}`），所以一个没有 a/b 的 Mapping 对"两臂各自记了几个变量"
    这件事**什么都没说**——`{}` 因为 `all([])` 恒真而被读成"证据充分"，
    `{'x': 3}` 因为键无关而被读成"两侧都记了"，两者都拿到了 `变量已对齐。mycode 多过 6 题`。
    空证据不是充分证据；这正是本模块要区分的那两件事在代码里又被合并了一次。
    """
    assert _guard_was_blind({}) is True                        # 键缺失 = 没校验过
    assert _guard_was_blind({"variables_seen": None}) is True   # 不是 Mapping 同样算缺失
    assert _guard_was_blind({"variables_seen": {"a": 0, "b": 1}}) is True  # 一侧没记 = 没法比对
    assert _guard_was_blind({"variables_seen": {"a": 1, "b": 0}}) is True
    assert _guard_was_blind({"variables_seen": {"a": 0, "b": 0}}) is True  # 1.86× 那次的形状
    assert _guard_was_blind({"variables_seen": {"a": 2, "b": 4}}) is False  # 真校验过
    # N2 新增三条。
    assert _guard_was_blind({"variables_seen": {}}) is True       # 空证据 ≠ 充分证据（all([]) 恒真）
    assert _guard_was_blind({"variables_seen": {"x": 3}}) is True  # 没有 a/b = 对两臂什么都没说
    # 简报写的是 `is True`，实测与它自己给的实现相反：`not (seen.get("a") and seen.get("b"))`
    # 在 `{'a':1,'b':1,'c':0}` 上是 False——`c` 不是臂，文档承诺的规则是"a/b 两侧都非 0"。
    # 复审 §4 N2 的表格同一行也写着"不该判瞎"。见报告 §6 E1。
    assert _guard_was_blind({"variables_seen": {"a": 1, "b": 1, "c": 0}}) is False


def test_the_blind_reason_never_speaks_without_a_subject():
    """钉住 `_guard_was_blind` 与 `_guard_blind_reason` 的**口径一致**这条不变量。

    N2 之前两者对同一份数据的读法不同：判据是键无关的 `all(seen.values())`，
    原因串却按键相关的 `seen.get("a")` / `seen.get("b")` 说话。后果是
    `{'a':1,'b':1,'c':0}` 被判瞎、却走到了那条注释写着"走不到"的第 4 支，
    印出一句**没有主语**的 `没有记录控制变量`——既不是"都"也不是"一侧"，
    读者无从知道是谁没记（复审 §4 N2 实测）。

    N2 之后：判瞎 ⟺ a/b 至少一侧取不到非 0 值 ⟺ `empty` 非空 ⟺ 原因串有主语。
    所以第 4 支改成 `raise AssertionError`——一条永不为真的注释不如一条会炸的断言。
    选 raise 而不是 `return ""`：空串会渲染成 `⚠️ 本次对比未经过变量校验（）：…`，
    即"守卫警告但没有理由"，而"说清为什么瞎"正是本模块存在的理由；
    而且 `python -O` 只剥 `assert`、不剥 `raise AssertionError`，不变量不会跟着
    优化级别消失。生产侧两个调用点（`headline` / `render_markdown`）都在
    `_guard_was_blind` 之后才调本函数，所以这一支在生产路径上走不到。
    """
    blind_shapes = [
        None,                       # 键缺失（compare_rows 那一层）
        {},                         # 空证据
        {"x": 3},                   # 没有 a/b 键
        {"a": 0, "b": 0},           # 1.86× 那次的形状
        {"a": 0, "b": 2},           # 一侧没记
        {"a": 8, "b": 0},           # 盘上已发表那一对的形状
        {"a": None, "b": 2},        # 键在、值不是数字
    ]
    for seen in blind_shapes:
        comparison = {} if seen is None else {"variables_seen": seen}
        assert _guard_was_blind(comparison) is True, f"{seen} 该判瞎"
        reason = _guard_blind_reason(comparison, "mycode", "opencode")
        assert reason, f"{seen} 判瞎却没有原因串——沉默的守卫等于没有守卫"
        # "谁没记录"这类原因必须说清主语和范围。第 1 支（compare_rows 那一层）不在此列：
        # 它不指责任何一臂，只说清"这一层无从校验"，那一句在它自己的调用层为真。
        if "没有记录控制变量" in reason:
            assert "mycode" in reason or "opencode" in reason, f"{seen} 的原因串没有主语：{reason}"
            assert "都" in reason or "一侧" in reason, f"{seen} 的原因串没说清是一侧还是两侧：{reason}"

    # 反过来：判**不**瞎的形状不许有人来问"为什么瞎"，第 4 支必须炸。
    for seen in [{"a": 1, "b": 1}, {"a": 2, "b": 4}, {"a": 1, "b": 1, "c": 0}]:
        assert _guard_was_blind({"variables_seen": seen}) is False, f"{seen} 不该判瞎"
        with pytest.raises(AssertionError):
            _guard_blind_reason({"variables_seen": seen}, "mycode", "opencode")


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


# ── 修复轮 2：守卫在「既未对齐、又瞎」这个形状下不许把未知说成已知不一致 ──
#
# 复审查出（§3.7 / §4 N3）：`headline` 的分支顺序是「先 mismatches 再 blind」，
# 于是「既未对齐、又瞎」时只说未对齐、一个字不提瞎。盘上已发表那一对
# （harbor_mycode_final_20261018__025351 vs harbor_opencode_final_20261017__232016）
# 正是这个形状：variables_seen={'a':8,'b':0}、8 项 mismatch **全部**是 b 侧 None，
# 其中就有 variables.thinking_mode——1.86× 事故里没被记下的正是这个变量。
# 而修复前 headline 说的是「变量未对齐（8 项不同）」：把**未知**报成了**已知不一致**。
#
# 这个重叠状态在 f804932 时不可能出现（那时 `_guard_was_blind` 用 `any`，
# `{'a':8,'b':0}` 判不瞎），是修复轮 1 把 `any` 改成 `all` 之后新造出来的。

# 盘上已发表那一份 mycode 报告的 `summary.variables` 键集（只读核过，8 个键）。
PUBLISHED_ARM_VARIABLES = {
    "model": "qwen3.6-27b",
    "seed": 20261017,
    "git_revision": "ab6b631-dirty",
    "mycode_env": {"MYCODE_THINKING_FEEDBACK": "false"},
    "provider": "openrouter",
    "stage": "final",
    "thinking_effective": "跟随模型默认（未注入）",
    "thinking_mode": "default",
}


def test_an_unaligned_and_blind_comparison_says_both_things():
    """一侧没记录时，不许把「未知」说成「两侧取值不同」，也不许把「瞎」咽掉。

    盘上已发表那一对（mycode_final vs opencode_final）就是这个形状：
    variables_seen={'a':8,'b':0}、8 项 mismatch 全是 b 侧 None，
    其中包含 variables.thinking_mode——1.86× 事故里没被记下的正是这个变量。

    夹具走的是真的 `compare_reports`（legacy 报告形状：a 侧 8 个变量、b 侧一个都没有），
    实测得到的 mismatch 字段与顺序和盘上那一对逐项一致，所以这条测试守的就是那个事故。
    """
    a = _legacy_report([_legacy_trial("t", 1.0)],
                       variables=dict(PUBLISHED_ARM_VARIABLES), arm="mycode")
    b = _legacy_report([_legacy_trial("t", 0.0)], arm="opencode")  # summary 里没有 variables 键
    result = compare_reports(a, b)

    # 先钉夹具形状：形状不对，这条测试守的就不是那个事故了
    assert result["variables_seen"] == {"a": 8, "b": 0}
    assert len(result["variable_mismatches"]) == 8
    assert all(m["b"] is None for m in result["variable_mismatches"])  # 全是"一侧没记录"
    fields = [m["field"] for m in result["variable_mismatches"]]
    assert "variables.thinking_mode" in fields
    assert fields == ["meta.model", "meta.seed", "meta.git_revision", "variables.mycode_env",
                      "variables.provider", "variables.stage", "variables.thinking_effective",
                      "variables.thinking_mode"]

    text = headline(result)
    # 金标准串：逐字符相等，不是子串
    assert text == (
        "变量未对齐（8 项不同，其中 8 项是一侧没记录、不是两侧取值不同）："
        "mycode 1/1 vs opencode 0/1，McNemar p = 1.0。"
        "⚠️ 本次对比未经过变量校验（opencode 一侧没有记录控制变量）。"
        "下面只是描述性数字，不构成结论。"
    )
    # 两件事必须同时说出来
    assert "变量未对齐" in text and "未经过变量校验" in text
    # 「未知」不许被说成「已知不一致」
    assert "8 项是一侧没记录、不是两侧取值不同" in text
    for phrase in WINNER_PHRASES:
        assert phrase not in text, f"既未对齐又瞎却出现了胜者句式：{phrase}"
    for word in SUPERLATIVES:
        assert word not in text, f"既未对齐又瞎却出现了结论性措辞：{word}"

    # markdown 顶部横幅**同样**带上这两件事——横幅是人眼里的唯一提示
    lines = render_markdown(result).splitlines()
    banners = [i for i, ln in enumerate(lines) if ln.startswith(">") and "⚠️" in ln]
    assert len(banners) == 2, f"该有未对齐与未经校验两条横幅，实际在 {banners}"
    assert lines[banners[0]] == "> ⚠️ **变量未对齐**——下面所有数字都是描述性的，不构成结论。"
    assert lines[banners[1]] == "> ⚠️ 本次对比未经过变量校验（opencode 一侧没有记录控制变量）。"
    assert banners[0] <= 3, f"横幅掉到第 {banners[0] + 1} 行，第一屏看不见"

    # 差异表里「一侧没记录」必须写「未记录」，不是字面量 `None`——
    # 印 `` `None` `` 会让读者读成「opencode 的 model 值是 None」，同一个毛病的人眼版。
    table = [ln for ln in lines if ln.startswith("| `")]
    assert len(table) == 8
    assert all(ln.endswith("| 未记录 |") for ln in table), table
    assert not any("`None`" in ln for ln in table), table
    assert "| `variables.thinking_mode` | `default` | 未记录 |" in lines


def test_every_guard_blind_reason_is_a_golden_string():
    """守卫为什么瞎，这句话本身也是安全关键输出——4 条返回路径全部逐字符钉死。

    修复轮 1 的 D1 把守卫那句话从"1 个固定模板"拆成
    `GUARD_BLIND_WARNING（{原因}）` + 4 选 1 的原因串，而金标准串只钉住了
    "都…"那一条。另外三条只有子串断言（`"未经过变量校验" in text`）+ 禁词表看守，
    而禁词表按 Change 2 自己的论证是**列不全**的：复审重造的 M7/M8 两个变异
    就是往未钉的那两条分支里追加 `。mycode 快 1.86×`，51 条测试全绿存活——
    M8 存活的位置正是 Critical 本体那条 `compare_rows` 路径，
    M7 存活的形状 `{'a': 0, 'b': 2}` 正是 1.86× 事故自己的形状。
    `快 1.86×` 就是当年真的写进用户汇报的那句措辞。

    这条测试与上面两条端到端金标准串是**互补**的：这条直接钉原因串本身
    （4 条路径一处不漏），那两条钉原因串被拼进 headline / 横幅之后的完整句子
    （防拼接处被改）。
    """
    # 1) seen 不是 Mapping —— compare_rows 那一层，看不到报告（M8 存活的那条）
    assert _guard_blind_reason({}, "mycode", "opencode") == (
        "这一层只看得到逐题行、看不到报告，无从校验控制变量"
    )
    assert _guard_blind_reason({"variables_seen": None}, "mycode", "opencode") == (
        "这一层只看得到逐题行、看不到报告，无从校验控制变量"
    )

    # 2) 两侧都空
    assert _guard_blind_reason({"variables_seen": {"a": 0, "b": 0}}, "mycode", "opencode") == (
        "mycode 与 opencode 都没有记录控制变量"
    )
    # `{}` 与 `{'x': 3}` 在 N2 之后也判瞎，落到同一支：没有 a/b 就等于两侧都没记
    assert _guard_blind_reason({"variables_seen": {}}, "mycode", "opencode") == (
        "mycode 与 opencode 都没有记录控制变量"
    )
    assert _guard_blind_reason({"variables_seen": {"x": 3}}, "mycode", "opencode") == (
        "mycode 与 opencode 都没有记录控制变量"
    )

    # 3) 一侧空 —— {'a': 0, 'b': 2} 正是 1.86× 事故的形状（M7 存活的那条）
    assert _guard_blind_reason({"variables_seen": {"a": 0, "b": 2}}, "mycode", "opencode") == (
        "mycode 一侧没有记录控制变量"
    )
    assert _guard_blind_reason({"variables_seen": {"a": 8, "b": 0}}, "mycode", "opencode") == (
        "opencode 一侧没有记录控制变量"
    )
    # 标签是参数，原因串必须跟着标签走，不许写死臂名
    assert _guard_blind_reason({"variables_seen": {"a": 0, "b": 2}}, "左臂", "右臂") == (
        "左臂 一侧没有记录控制变量"
    )

    # 4) N2 之后第 4 支走不到：a/b 都取到非 0 值就不算瞎，谁都不该来问"为什么瞎"。
    #    它必须**炸**，而不是印一句没有主语的 `没有记录控制变量`
    #    （复审实测 `{'a':1,'b':1,'c':0}` 曾走到这一支）。
    for seen in [{"a": 1, "b": 1}, {"a": 2, "b": 4}, {"a": 1, "b": 1, "c": 0}]:
        with pytest.raises(AssertionError):
            _guard_blind_reason({"variables_seen": seen}, "mycode", "opencode")


def test_the_documented_self_exemption_hatch_is_pinned_end_to_end():
    """`compare_rows` 的 docstring 公开推荐"自己把 variables_seen 塞进结果 dict"，
    而修复前全文件**没有任何一条测试真的这么做过**（复审 grep 确认）——有文档、无测试。

    这条的价值不是修 bug（分支逻辑本来就对），是把这条公开推荐的路径钉住：
    免得下一个人以为口子坏了而自己去手搓渲染器，或者以为口子比文档写的更宽。
    修复前实测（复审 §3.6）：塞 `{}`、塞 `{'x': 3}` 都能拿到
    `变量已对齐。mycode 多过 6 题（…）`——空证据被当成了充分证据，N2 修掉的就是这个。

    规则以 docstring 为准：**`a` / `b` 两个键都取到非 0 的值**才算校验过。
    """
    base = compare_rows(
        [_row(f"t{i}", True) for i in range(6)],
        [_row(f"t{i}", False) for i in range(6)],
        labels=("mycode", "opencode"),
    )
    assert "variables_seen" not in base            # 这一层确实算不出来
    assert base["variable_mismatches"] == []       # compare_rows 硬编码为空

    # 1) 正当用途：调用方确实在别处校验过 → 允许给胜者结论（要钉住它**能用**）
    exempt = dict(base, variables_seen={"a": 2, "b": 2})
    assert headline(exempt) == (
        "变量已对齐。mycode 多过 6 题（mycode 6/6 vs opencode 0/6，McNemar p = 0.0312）"
    )

    # 2) 空证据不是充分证据：`{}` 必须判瞎（`all([])` 恒真是 N2 修掉的那个洞）
    empty = dict(base, variables_seen={})
    assert headline(empty) == (
        "⚠️ 本次对比未经过变量校验（mycode 与 opencode 都没有记录控制变量）："
        "mycode 6/6 vs opencode 0/6，McNemar p = 0.0312。"
        "下面只是描述性数字，不构成结论。"
    )

    # 3) 没有 a/b 键的 Mapping 对"两臂各自记了几个变量"什么都没说 → 同样判瞎
    offkeys = dict(base, variables_seen={"x": 3})
    assert headline(offkeys) == headline(empty)

    # 4) 一侧为 0 → 判瞎，且原因串有主语（说清是谁没记）
    onesided = dict(base, variables_seen={"a": 2, "b": 0})
    assert headline(onesided) == (
        "⚠️ 本次对比未经过变量校验（opencode 一侧没有记录控制变量）："
        "mycode 6/6 vs opencode 0/6，McNemar p = 0.0312。"
        "下面只是描述性数字，不构成结论。"
    )

    # 三种空证据一律不许出现胜者措辞
    for shape in (empty, offkeys, onesided):
        text = headline(shape)
        for phrase in WINNER_PHRASES:
            assert phrase not in text, f"{shape['variables_seen']} 却出现了胜者句式：{phrase}"
        for word in SUPERLATIVES:
            assert word not in text, f"{shape['variables_seen']} 却出现了结论性措辞：{word}"
        assert "变量已对齐" not in text, f"{shape['variables_seen']} 判瞎却自称已对齐"

    # 5) 额外的键不改变判定：文档承诺的规则是"a/b 两侧都非 0"，`c` 不是臂。
    #    复审 §4 N2 的表格与它自己的"最小修法"文字在这一点上互相矛盾，
    #    以它给的那行实现为准（`not (seen.get("a") and seen.get("b"))`）——
    #    也只有这样 `_guard_blind_reason` 的第 4 支才真的走不到。见报告 §6 E1。
    extrakey = dict(base, variables_seen={"a": 1, "b": 1, "c": 0})
    assert headline(extrakey) == headline(exempt)


def test_two_arms_may_not_collapse_into_the_same_label():
    """`_label_of` 的倒数第二层回退是 `benchmark`，两份同 benchmark 又没有
    `run_id` / `meta.arm` / `summary.arm` 的报告会让两侧标签**都是** `"harbor"`。

    修复前实测：`labels == {'a': 'harbor', 'b': 'harbor'}`，markdown 标题渲染成
    `# harbor vs harbor`、headline 渲染成 `harbor 6/6 vs harbor 0/6`——
    读者无从分辨谁是谁，而这句话读起来仍然像一个结论。`?` 那一层同样会撞。

    去重只加在 `compare_reports` 上，**不改 `_label_of` 的四层回退链**
    （上一轮 Change 3 刚钉过它，顺序有测试看守）。
    """
    a = {"benchmark": "harbor", "results": [_row(f"t{i}", True) for i in range(6)]}
    b = {"benchmark": "harbor", "results": [_row(f"t{i}", False) for i in range(6)]}

    result = compare_reports(a, b)

    assert result["labels"] == {"a": "harbor (a)", "b": "harbor (b)"}
    assert result["labels"]["a"] != result["labels"]["b"], "两臂标签撞成同名，读者无从分辨谁是谁"
    assert render_markdown(result).splitlines()[0] == "# harbor (a) vs harbor (b)"
    # headline 里两个臂也必须可区分（复审实测修复前是 `harbor 6/6 vs harbor 0/6`）
    assert "harbor (a) 6/6 vs harbor (b) 0/6" in headline(result)
    # 回退链本身一个字没动
    assert _label_of(a) == "harbor" and _label_of(b) == "harbor"

    # 两侧标签本来就不同时，一个字都不许加（否则会污染所有正常报告）
    distinct = compare_reports(
        _report([_row("t", True)], run_id="mycode", **ALIGNED),
        _report([_row("t", False)], run_id="opencode", **ALIGNED),
    )
    assert distinct["labels"] == {"a": "mycode", "b": "opencode"}

    # 显式 `labels=` 是调用方的原话，上一轮 Change 3 钉过"必须原样到达两个渲染"
    # （`test_explicit_labels_reach_both_renders_verbatim`），所以**不去重**：
    # 悄悄改写调用方传进来的字符串，比读者看见两个同名标签更坏——
    # Task 13 的路由走的正是显式 labels=，它得能拿回自己传进去的东西。
    explicit = compare_reports(a, b, labels=("harbor", "harbor"))
    assert explicit["labels"] == {"a": "harbor", "b": "harbor"}


# ── Task 12 修复轮 1（I-2）：横幅措辞抽成公开的 `guard_banner`，只此一份 ──

# harbor 那一层原来自己写着「两臂的控制变量不一致」，通用层写着「变量未对齐」——
# 安全关键措辞有两份，而且已经开始漂移（harbor 少了「对齐之后重跑」那句补救提示）。
# 抽提是**纯搬移**：下面第一条测试钉住"通用层自己也没有第二份"，
# harbor 那侧由 `test_harbor_compare.py::test_the_guard_banner_is_the_common_layers_word_for_word`
# 钉住"两个渲染器逐字节相同"。横幅本身的文字仍由上面几条金标准串逐字符看守，
# 所以搬错一个字就红——这次抽提是自验证的。


def test_render_markdowns_banner_is_guard_banners_output_verbatim():
    """`render_markdown` 顶部的横幅必须**逐行等于** `guard_banner` 的输出。

    四个形状全过（含"守卫没开火"）。哪天有人把横幅重新内联回 `render_markdown`、
    或在别处再拼一份，两边一分叉这条就红——那正是 I-2 要防的事。
    """
    blind_a = _report([_row("t", True)], run_id="mycode", **ALIGNED)
    blind_b = _report([_row("t", False)], run_id="opencode", **ALIGNED)
    blind_a["meta"]["variables"] = {}
    blind_b["meta"]["variables"] = {}
    shapes = {
        "未对齐": compare_reports(
            _report([_row(f"t{i}", True) for i in range(6)], run_id="mycode", model="m1", **ALIGNED),
            _report([_row(f"t{i}", False) for i in range(6)], run_id="opencode", model="m2", **ALIGNED),
        ),
        "既未对齐又瞎": compare_reports(
            _legacy_report([_legacy_trial("t", 1.0)],
                           variables=dict(PUBLISHED_ARM_VARIABLES), arm="mycode"),
            _legacy_report([_legacy_trial("t", 0.0)], arm="opencode"),
        ),
        "只是瞎": compare_reports(blind_a, blind_b),
        "对齐（守卫不开火）": compare_reports(
            _report([_row("t", True)], run_id="mycode", **ALIGNED),
            _report([_row("t", True)], run_id="opencode", **ALIGNED),
        ),
    }

    for name, result in shapes.items():
        label_a, label_b = result["labels"]["a"], result["labels"]["b"]
        banner = guard_banner(result, label_a=label_a, label_b=label_b)
        lines = render_markdown(result).splitlines()
        assert lines[:2] == [f"# {label_a} vs {label_b}", ""], name
        assert lines[2 : 2 + len(banner)] == banner, name
        # 横幅之后紧接着就是 headline：中间不许塞东西，否则横幅掉出第一屏
        assert lines[2 + len(banner)] == headline(result), name

    # 守卫没开火时一个字都不许打：永远挂着的警告等于没有警告
    aligned = shapes["对齐（守卫不开火）"]
    assert guard_banner(aligned, label_a=aligned["labels"]["a"], label_b=aligned["labels"]["b"]) == []


def test_guard_banner_handles_a_comparison_without_variables_seen():
    """`variables_seen` 这个键在行层面的结果字典里**根本不存在**（harbor 的 `compare()`
    就是那个形状，它刻意不补假值）。键缺失必须判瞎、照样打横幅，不许 KeyError、
    更不许沉默——"什么都没校验"和"校验通过"在数据上长得一模一样。
    """
    assert guard_banner({"variable_mismatches": []}, label_a="mycode", label_b="opencode") == [
        "> ⚠️ 本次对比未经过变量校验（这一层只看得到逐题行、看不到报告，无从校验控制变量）。",
        "> 下面所有数字都是描述性的，不构成结论。",
        "",
    ]
    # 连 `variable_mismatches` 都没有也得能走（`_guard_was_blind` 读的是 `.get`）
    assert guard_banner({}, label_a="mycode", label_b="opencode")[0].startswith(
        f"> {GUARD_BLIND_WARNING}"
    )
    # 未对齐 + 键缺失 → 两件事都要说，单元格仍是「未记录」不是 `` `None` ``
    both = guard_banner(
        {"variable_mismatches": [{"field": "meta.model", "a": "m1", "b": None}]},
        label_a="mycode",
        label_b="opencode",
    )
    assert both[0] == "> ⚠️ **变量未对齐**——下面所有数字都是描述性的，不构成结论。"
    assert both[1] == "> ⚠️ 本次对比未经过变量校验（这一层只看得到逐题行、看不到报告，无从校验控制变量）。"
    assert "| `meta.model` | `m1` | 未记录 |" in both


def test_the_banner_prose_is_safe_to_echo_to_stdout():
    """harbor 的 CLI 会把横幅里 `> ` 开头的几句原样回显到 stdout（去掉引用标记与粗体）。

    stdout 是人跑 CLI 唯一一定会读到的输出，所以那几句同样不许带任何胜者措辞。
    禁词表**只在本文件存一份**：harbor 的测试不抄第二份，它钉的是
    "stdout 的那几行逐行等于横幅里 `> ` 开头的那几句"，于是这条一红、那边跟着红。
    """
    result = compare_reports(
        _legacy_report([_legacy_trial("t", 1.0)],
                       variables=dict(PUBLISHED_ARM_VARIABLES), arm="mycode"),
        _legacy_report([_legacy_trial("t", 0.0)], arm="opencode"),
    )
    echoed = [
        line.removeprefix("> ").replace("**", "")
        for line in guard_banner(result, label_a="mycode", label_b="opencode")
        if line.startswith("> ")
    ]

    # 三句：未对齐 + 未经校验（盘上已发表那一对正是这个重叠形状）+ 补救提示
    assert echoed == [
        "⚠️ 变量未对齐——下面所有数字都是描述性的，不构成结论。",
        "⚠️ 本次对比未经过变量校验（opencode 一侧没有记录控制变量）。",
        "对齐之后重跑，或者只把它当线索。",
    ]
    for line in echoed:
        assert line.strip(), "回显到 stdout 的空行等于没说话"
        for word in SUPERLATIVES:
            assert word not in line, f"守卫开火时 stdout 出现了结论性措辞：{word}"
        for phrase in WINNER_PHRASES:
            assert phrase not in line, f"守卫开火时 stdout 出现了胜者句式：{phrase}"
    # 三句里必须有一句明确拒绝下结论（另两句分别是"未对齐"的事实与"守卫瞎了"的事实）
    assert any("不构成结论" in line for line in echoed), echoed
    assert any("当线索" in line for line in echoed), echoed
