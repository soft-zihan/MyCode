"""两臂配对对比：这里的错误会让「没差别」和「有差别」互相冒充。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from eval.harbor.compare import (
    by_language,
    by_task,
    compare,
    find_latest_report,
    load_report,
    mcnemar_exact,
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


# ── McNemar 精确检验 ──────────────────────────────────────────────────


def test_mcnemar_of_no_discordance_is_one():
    # 两臂判定完全一致时没有信息，p 必须是 1，不能是 0
    assert mcnemar_exact(0, 0) == 1.0


def test_mcnemar_matches_the_reference_value():
    # b=8, c=2 → 2 * (C(10,0)+C(10,1)+C(10,2)) / 2^10 = 0.109375
    assert mcnemar_exact(8, 2) == pytest.approx(0.109375)


def test_mcnemar_is_symmetric():
    # 谁是「左臂」不该影响 p 值
    assert mcnemar_exact(8, 2) == mcnemar_exact(2, 8)


def test_mcnemar_never_exceeds_one():
    # b=c=1 时裸算出来是 1.5，必须截到 1.0
    assert mcnemar_exact(1, 1) == 1.0


def test_mcnemar_gets_small_when_discordance_is_one_sided():
    assert mcnemar_exact(10, 0) == pytest.approx(2 / 2**10)
    assert mcnemar_exact(10, 0) < mcnemar_exact(6, 4)


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


def test_by_task_keeps_the_latest_attempt_of_a_repeated_task():
    trials = [trial("t", 0.0), trial("t", 1.0)]

    assert by_task(trials)["t"]["reward"] == 1.0


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
