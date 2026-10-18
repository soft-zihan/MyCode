"""两臂配对对比：这里的错误会让「没差别」和「有差别」互相冒充。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from eval.common.compare import GUARD_BLIND_WARNING, rows_of
from eval.common.schema import normalize_row
from eval.harbor.compare import (
    ARM_KEYS,
    by_language,
    compare,
    compare_run_reports,
    drop_expected_mismatches,
    find_latest_report,
    load_report,
    pair_tasks,
    passed,
    render_markdown,
)


def trial(name: str, reward: float | None, exception: str | None = None, **extra) -> dict:
    return {
        "task_name": name,
        "reward": reward,
        "exception_type": exception,
        "n_input_tokens": extra.get("inp", 100),
        "n_output_tokens": extra.get("out", 10),
        "agent_execution_s": extra.get("secs", 30.0),
    }


# McNemar 精确检验的 5 条测试搬到了 tests/unit/test_eval_compare.py（实现也随之搬到
# eval/common/compare.py）。这里不留第二份：两份断言各自漂移，两臂对比和跨 benchmark
# 对比就会给出不一样的 p 值，而两边都"看起来对"，没人会发现。


# ── 出异常的题不能当成「没通过」 ─────────────────────────────────────


def test_passed_distinguishes_failed_from_unscored():
    assert passed(trial("t", 1.0)) is True
    assert passed(trial("t", 0.0)) is False
    # 出异常且没有 reward = 根本没跑起来，混进分母会把基础设施故障算成模型能力差
    assert passed(trial("t", None, exception="Boom")) is None
    assert passed(trial("t", None)) is None


def test_passed_counts_a_build_failure_as_a_real_failure():
    # cpp 构建不过时 verifier 写不出 reward 文件；collect_trials 已经把它按 0 分
    # 记进 reward 了，所以这里必须判 False 而不是 None——否则难题被剔出分母，
    # 做得差的那一臂通过率反而虚高
    assert passed(trial("t", 0.0, exception="RewardFileNotFoundError")) is False


def test_compare_pairs_a_build_failure_against_a_real_pass():
    mine = [trial("polyglot_cpp_x", 0.0, exception="RewardFileNotFoundError")]
    theirs = [trial("polyglot_cpp_x", 1.0)]

    result = compare(mine, theirs)

    assert result["n_paired"] == 1
    assert result["n_unscored"] == 0
    assert result["only_opencode"] == ["polyglot_cpp_x"]


def test_pair_tasks_matches_by_name_and_keeps_single_arm_tasks():
    pairs = pair_tasks([trial("a", 1.0)], [trial("b", 0.0)])

    assert [p["task_name"] for p in pairs] == ["a", "b"]
    assert pairs[0]["opencode_passed"] is None
    assert pairs[1]["mycode_passed"] is None


def test_pair_tasks_extracts_the_language():
    pairs = pair_tasks([trial("polyglot_rust_luhn-from", 1.0)], [])

    assert pairs[0]["language"] == "rust"


# ── 汇总 ─────────────────────────────────────────────────────────────


def test_compare_counts_only_the_scored_pairs():
    mine = [trial("a", 1.0), trial("b", 0.0), trial("c", None, exception="Boom")]
    theirs = [trial("a", 0.0), trial("b", 0.0), trial("c", 1.0)]

    result = compare(mine, theirs)

    # c 在 MyCode 侧没判分，整对都不进配对统计
    assert result["n_paired"] == 2
    assert result["n_unscored"] == 1
    assert result["unscored_tasks"] == ["c"]
    assert result["mycode_passed"] == 1
    assert result["opencode_passed"] == 0
    assert result["mycode_pass_rate"] == 0.5
    assert result["opencode_pass_rate"] == 0.0


def test_compare_names_the_discordant_tasks():
    mine = [trial("win", 1.0), trial("lose", 0.0), trial("tie", 1.0)]
    theirs = [trial("win", 0.0), trial("lose", 1.0), trial("tie", 1.0)]

    result = compare(mine, theirs)

    assert result["only_mycode"] == ["win"]
    assert result["only_opencode"] == ["lose"]
    # 1:1 的不一致没有方向性证据
    assert result["mcnemar_p"] == 1.0


def test_compare_of_nothing_reports_unknown_not_zero():
    result = compare([], [])

    assert result["n_paired"] == 0
    assert result["mycode_pass_rate"] is None
    assert result["mycode_mean_seconds"] is None


def test_compare_averages_tokens_and_time_over_scored_pairs_only():
    mine = [trial("a", 1.0, inp=100, out=10, secs=20.0), trial("b", None, exception="Boom", inp=999)]
    theirs = [trial("a", 1.0, inp=200, out=30, secs=40.0), trial("b", 1.0, inp=999)]

    result = compare(mine, theirs)

    assert result["mycode_mean_input_tokens"] == 100
    assert result["opencode_mean_input_tokens"] == 200
    assert result["mycode_mean_seconds"] == 20.0
    assert result["opencode_mean_seconds"] == 40.0


def test_exclude_language_drops_the_whole_language_from_the_pairing():
    # java 题在本环境里两臂都必然 0（Gradle wrapper TLS 验不过），留着会同时压低
    # 两边通过率、还让「java」这个维度看起来像测到了东西
    mine = [trial("polyglot_java_a", 0.0), trial("polyglot_rust_b", 1.0)]
    theirs = [trial("polyglot_java_a", 0.0), trial("polyglot_rust_b", 0.0)]

    result = compare(mine, theirs, exclude_languages=["java"])

    assert result["excluded_languages"] == ["java"]
    assert result["n_paired"] == 1
    assert [p["task_name"] for p in result["pairs"]] == ["polyglot_rust_b"]
    # 剔掉同向失败的题之后，剩下的那一道就是不一致对，p 值随之变化
    assert result["only_mycode"] == ["polyglot_rust_b"]


def test_exclude_language_that_is_absent_is_a_noop():
    mine = [trial("polyglot_rust_b", 1.0)]

    result = compare(mine, mine, exclude_languages=["java"])

    assert result["excluded_languages"] == []
    assert result["n_paired"] == 1


def test_render_markdown_declares_the_exclusion():
    result = compare(
        [trial("polyglot_java_a", 0.0), trial("polyglot_rust_b", 1.0)],
        [trial("polyglot_java_a", 0.0), trial("polyglot_rust_b", 1.0)],
        exclude_languages=["java"],
    )
    text = render_markdown(result)

    # 剔数据必须在报告里显眼地写出来，否则读的人以为是 25 题全测了
    assert "已剔除语言：java" in text
    assert "polyglot_java_a" not in text


def test_by_language_stratifies_the_pairs():

    pairs = pair_tasks(
        [trial("polyglot_rust_a", 1.0), trial("polyglot_rust_b", 0.0), trial("polyglot_go_c", 1.0)],
        [trial("polyglot_rust_a", 0.0), trial("polyglot_rust_b", 0.0), trial("polyglot_go_c", 1.0)],
    )

    table = by_language(pairs)

    assert table == {"go": {"n": 1, "mycode": 1, "opencode": 1}, "rust": {"n": 2, "mycode": 1, "opencode": 0}}


def test_by_language_skips_unscored_pairs():
    pairs = pair_tasks([trial("polyglot_go_c", None, exception="Boom")], [trial("polyglot_go_c", 1.0)])

    assert by_language(pairs) == {}


# ── 报告 ─────────────────────────────────────────────────────────────


def test_render_markdown_shows_the_p_value_and_every_task():
    result = compare([trial("polyglot_go_a", 1.0)], [trial("polyglot_go_a", 0.0)])
    text = render_markdown(result)

    assert "McNemar" in text and "p = 1.0" in text
    assert "| polyglot_go_a | 过 | 没过 |" in text
    assert "| go | 1 | 1 | 0 |" in text


def test_render_markdown_marks_unscored_trials_with_a_question_mark():
    result = compare([trial("polyglot_go_a", None, exception="Boom")], [trial("polyglot_go_a", 1.0)])

    assert "| polyglot_go_a | ? | 过 |" in render_markdown(result)


def test_find_latest_report_picks_the_newest_stamp(tmp_path):
    for stamp in ("20260101__000000", "20261017__020821", "20260601__120000"):
        (tmp_path / f"harbor_mycode_probe_{stamp}.json").write_text("{}", encoding="utf-8")
    (tmp_path / "harbor_opencode_probe_20270101__000000.json").write_text("{}", encoding="utf-8")
    (tmp_path / "unrelated.json").write_text("{}", encoding="utf-8")

    found = find_latest_report("mycode", "probe", tmp_path)

    # 别的臂、别的阶段、别的文件都不能被选中
    assert found.name == "harbor_mycode_probe_20261017__020821.json"


def test_find_latest_report_says_what_to_run_when_there_is_none(tmp_path):
    with pytest.raises(FileNotFoundError) as exc:
        find_latest_report("mycode", "final", tmp_path)

    assert "eval.harbor.runner" in str(exc.value)


def test_load_report_round_trips(tmp_path):
    path = tmp_path / "r.json"
    path.write_text(json.dumps({"summary": {"arm": "mycode"}, "trials": []}), encoding="utf-8")

    assert load_report(path)["summary"]["arm"] == "mycode"


# ── 委托给通用层：统计只有一份 ───────────────────────────────────────


def test_the_statistics_come_from_the_common_layer():
    # 委托的意义就在于此：harbor 的 p 值必须和通用层逐位相同。
    # 哪天有人图方便在 harbor 这边又写一份 McNemar，这条会红
    from eval.common.compare import compare_rows, mcnemar_exact

    mine = [trial("a", 1.0), trial("b", 1.0), trial("c", 0.0)]
    theirs = [trial("a", 0.0), trial("b", 0.0), trial("c", 0.0)]
    rows = lambda items: [normalize_row(t, benchmark="harbor") for t in items]

    generic = compare_rows(rows(mine), rows(theirs), labels=("mycode", "opencode"))

    assert compare(mine, theirs)["mcnemar_p"] == generic["mcnemar_p"] == mcnemar_exact(2, 0)
    assert compare(mine, theirs)["only_mycode"] == generic["only_a"] == ["a", "b"]


def test_compare_reads_a_legacy_trials_report_and_a_new_results_report_alike(tmp_path):
    # 盘上 2026-10-17 那两轮是 trials 形状，重跑一次要 45 分钟网关时间，
    # 而 README §5.7 的结论就引自它们；新报告是 results 形状。两种都得能比
    legacy = {"summary": {"arm": "mycode"},
              "trials": [trial("polyglot_rust_a", 1.0), trial("polyglot_rust_b", 0.0)]}
    modern = {"benchmark": "harbor", "run_id": "r", "generated_at": "t",
              "meta": {}, "summary": {},
              "results": [normalize_row(trial("polyglot_rust_a", 1.0), benchmark="harbor"),
                          normalize_row(trial("polyglot_rust_b", 0.0), benchmark="harbor")]}

    assert compare(legacy["trials"], rows_of(modern))["n_paired"] == 2
    assert compare(rows_of(legacy), rows_of(modern))["only_mycode"] == []


# ── 变量守卫：沉默地给出一个看起来可信的结论是本模块唯一的严重失效 ──


def test_drop_expected_mismatches_drops_only_the_revision():
    # ARM_KEYS 里只能有"两臂本来就该不同"的那一项。多一条 = 守卫被悄悄拆掉；
    # 少一条 = 横幅永远挂着、而永远挂着的警告等于没有警告。两种都必须红。
    result = {
        "variable_mismatches": [
            {"field": "meta.git_revision", "a": "ab6b631-dirty", "b": None},
            {"field": "variables.thinking_mode", "a": "off", "b": "on"},
            {"field": "meta.model", "a": "qwen3.6-27b", "b": None},
        ]
    }

    kept = drop_expected_mismatches(result)["variable_mismatches"]

    assert [m["field"] for m in kept] == ["variables.thinking_mode", "meta.model"]
    # 死条目和 SUPERLATIVES 是同一类缺陷：一份看着在守、其实永远不触发的清单。
    # `arm` 不在 `run_variables()` 的 8 个键里，通用层也从不比 meta.arm/summary.arm，
    # 所以 "variables.arm" 永远产不出来——不许"留着以防万一"
    assert ARM_KEYS == frozenset({"meta.git_revision"})


def test_the_guard_banner_names_the_side_that_recorded_nothing():
    # README §5.7 那一对的形状：mycode 侧记了控制变量，opencode 侧一个都没记。
    # 横幅必须说清是「一侧没记录」，而不是一串看起来像取值的 `None`——
    # 把未知渲染成已知取值，正是「MyCode 快 1.9×」那个假结论的人眼版
    mine = {
        "benchmark": "harbor",
        "summary": {"arm": "mycode", "variables": {"model": "m", "thinking_mode": "off"}},
        "trials": [trial("polyglot_rust_a", 1.0), trial("polyglot_go_b", 1.0)],
    }
    theirs = {
        "benchmark": "harbor",
        "summary": {"arm": "opencode"},
        "trials": [trial("polyglot_rust_a", 0.0), trial("polyglot_go_b", 0.0)],
    }

    result = compare_run_reports(mine, theirs, mycode_label="mycode", opencode_label="opencode")
    text = render_markdown(result)

    assert [m["field"] for m in result["variable_mismatches"]] == [
        "meta.model",
        "variables.thinking_mode",
    ]
    assert "两臂的控制变量不一致" in text
    # 「既未对齐、又瞎」必须同时说出来，否则读者以为那几项差异是两臂都实测出来的
    assert GUARD_BLIND_WARNING in text
    assert "opencode 一侧没有记录控制变量" in text
    assert "| 未记录 |" in text
    assert "`None`" not in text
