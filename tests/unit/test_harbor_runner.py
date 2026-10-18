"""Harbor benchmark 的选题抽样：按语言均衡轮转，且前缀可嵌套。"""

from __future__ import annotations

import pytest

from eval.harbor.runner import STAGE_SIZES, select_tasks, stage_n_tasks, task_language


def _make_ids() -> list[str]:
    """6 种语言，题数与 aider-polyglot 实测一致：js49 java47 go39 py34 rust30 cpp26。"""
    counts = {
        "javascript": 49,
        "java": 47,
        "go": 39,
        "python": 34,
        "rust": 30,
        "cpp": 26,
    }
    ids: list[str] = []
    for lang, n in counts.items():
        ids.extend(f"polyglot_{lang}_ex{i}" for i in range(n))
    return ids


def test_task_language_parses_name():
    assert task_language("polyglot_python_two-bucket") == "python"
    assert task_language("polyglot_cpp_binary-search-tree") == "cpp"
    # exercise 名里带下划线也只取第二段
    assert task_language("polyglot_go_error_handling_extra") == "go"


def test_task_language_returns_none_for_other_shapes():
    assert task_language("hello-world") is None
    assert task_language("polyglot_python") is None
    assert task_language("") is None


def test_probe_size_gives_one_task_per_language():
    ids = _make_ids()

    picked = select_tasks(ids, 6, seed=20261016)

    assert len(picked) == 6
    assert sorted(task_language(t) for t in picked) == [
        "cpp", "go", "java", "javascript", "python", "rust",
    ]


def test_prefix_nesting_holds_between_probe_and_final():
    ids = _make_ids()

    probe = select_tasks(ids, 6, seed=20261016)
    final = select_tasks(ids, 25, seed=20261016)

    assert final[:6] == probe
    assert len(final) == 25
    assert len(set(final)) == 25


def test_the_full_stage_means_every_task_the_dataset_has():
    ids = _make_ids()

    assert STAGE_SIZES["full"] is None
    assert stage_n_tasks("full", len(ids)) == len(ids)
    assert len(select_tasks(ids, stage_n_tasks("full", len(ids)), seed=20261016)) == 225


def test_full_nests_the_final_batch_so_the_two_runs_can_be_pooled():
    """跑满那批的前 25 题必须就是 final 批——否则两次跑批的数据没法合并统计。"""
    ids = _make_ids()

    full = select_tasks(ids, stage_n_tasks("full", len(ids)), seed=20261016)

    assert full[:25] == select_tasks(ids, 25, seed=20261016)
    assert full[:6] == select_tasks(ids, 6, seed=20261016)


def test_fixed_stages_ignore_how_many_tasks_the_dataset_has():
    """题数是常量，不随数据集大小变；只有 full 跟着数据集走。"""
    assert stage_n_tasks("probe", 225) == 6
    assert stage_n_tasks("final", 225) == 25
    assert stage_n_tasks("final", 10) == 25
    assert stage_n_tasks("full", 10) == 10


def test_final_allocation_is_balanced_and_favours_big_languages():
    ids = _make_ids()

    picked = select_tasks(ids, 25, seed=20261016)

    counts: dict[str, int] = {}
    for t in picked:
        lang = task_language(t)
        counts[lang] = counts.get(lang, 0) + 1
    # 25 = 4 轮 × 6 语言 + 1，多出来的一个给题最多的 javascript
    assert counts == {
        "javascript": 5, "java": 4, "go": 4, "python": 4, "rust": 4, "cpp": 4,
    }


def test_same_seed_is_deterministic():
    ids = _make_ids()

    assert select_tasks(ids, 25, seed=7) == select_tasks(ids, 25, seed=7)


def test_different_seed_picks_different_tasks():
    ids = _make_ids()

    assert select_tasks(ids, 25, seed=1) != select_tasks(ids, 25, seed=2)


def test_input_order_does_not_matter():
    ids = _make_ids()
    shuffled = list(reversed(ids))

    assert select_tasks(ids, 25, seed=3) == select_tasks(shuffled, 25, seed=3)


def test_n_larger_than_pool_returns_everything():
    ids = _make_ids()

    assert len(select_tasks(ids, 999, seed=3)) == len(ids)


def test_exhausted_language_is_skipped_not_repeated():
    ids = ["polyglot_cpp_a", "polyglot_cpp_b", "polyglot_python_c"]

    picked = select_tasks(ids, 10, seed=3)

    assert len(picked) == 3
    assert len(set(picked)) == 3


def test_empty_input():
    assert select_tasks([], 6, seed=3) == []


def test_zero_n():
    assert select_tasks(_make_ids(), 0, seed=3) == []


def test_names_without_language_go_last_and_never_displace_real_tasks():
    ids = ["weird-name", "polyglot_python_a", "polyglot_cpp_b"]

    picked = select_tasks(ids, 2, seed=3)

    assert sorted(picked) == ["polyglot_cpp_b", "polyglot_python_a"]
    assert select_tasks(ids, 3, seed=3)[-1] == "weird-name"


def test_negative_n_raises():
    with pytest.raises(ValueError):
        select_tasks(_make_ids(), -1, seed=3)
