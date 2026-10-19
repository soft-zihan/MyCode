"""Harbor benchmark 的选题抽样：按语言均衡轮转，且前缀可嵌套。"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from eval.harbor import runner
from eval.harbor.runner import (
    SEED,
    STAGE_SIZES,
    completed_task_names,
    merge_trials,
    select_tasks,
    stage_n_tasks,
    task_language,
    write_report,
)


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


# ── 断点续跑（--resume-from）───────────────────────────────────────────
#
# 为什么有这个功能：一次两臂满跑要 17 小时，opencode 臂跑到第 2 题时 WSL 被重启、
# 进程死掉，而 runner 当时没有任何续跑手段，那一整臂只能从零重来。
# 夹具一律用 tmp_path 造假 job 目录：真的 ~/.mycode/harbor_jobs 与 eval/reports
# 里装着不可再生的基线数据（MyCode 臂那 8 小时 36 分），只读、绝不写。


def _write_trial(root: Path, dirname: str, task_name: str | None, reward: float | None = 1.0) -> Path:
    """造一个最小但形状真实的 trial 目录。

    核过 `collect_trials`：它只从 `result.json` 里**硬**读 `task_name`（缺了就是 None，
    于是这道题既不进 `completed_task_names` 也没法当合并键），其余键一律 `.get()` 兜：
    `verifier_result.rewards.reward` 缺失 → reward=None（未判分）；`agent_result` 缺失 →
    token 三个键都是 None；`exception_info` 缺失 → exception_type=None、no_reward_file=False；
    四个 phase（environment_setup / agent_setup / agent_execution / verifier）缺失或时间戳
    没有小数秒 → 对应的 *_s 是 None（`_phase_seconds` 的 strptime 格式带 `%f`，是必需位）。
    所以这里只写 task_name + reward，其余缺键的行为就是生产路径的行为。
    """
    trial_dir = root / dirname
    trial_dir.mkdir(parents=True, exist_ok=True)
    data: dict = {"trial_name": dirname}
    if task_name is not None:
        data["task_name"] = task_name
    if reward is not None:
        data["verifier_result"] = {"rewards": {"reward": reward}}
    (trial_dir / "result.json").write_text(json.dumps(data), encoding="utf-8")
    return trial_dir


def _make_dataset(root: Path, ids: list[str]) -> Path:
    """假数据集：`list_task_ids` 认的是「子目录里有 task.toml」。"""
    dataset = root / "dataset"
    for name in ids:
        task_dir = dataset / name
        task_dir.mkdir(parents=True, exist_ok=True)
        (task_dir / "task.toml").write_text("", encoding="utf-8")
    return dataset


def _ten_ids() -> list[str]:
    """10 道题、3 种语言（cpp 4 / go 3 / java 3），够让轮转顺序非平凡。"""
    return [
        "polyglot_cpp_alpha",
        "polyglot_go_beta",
        "polyglot_java_gamma",
        "polyglot_cpp_delta",
        "polyglot_go_epsilon",
        "polyglot_java_zeta",
        "polyglot_cpp_eta",
        "polyglot_go_theta",
        "polyglot_java_iota",
        "polyglot_cpp_kappa",
    ]


def _argv(
    dataset: Path,
    resume: Path | None,
    *,
    arm: str = "opencode",
    stage: str = "full",
    jobs: Path | None = None,
    configs: Path | None = None,
    prewarm: bool = False,
    extra: tuple[str, ...] = (),
) -> list[str]:
    argv = [
        "--arm", arm,
        "--stage", stage,
        "--dataset-dir", str(dataset),
        "--jobs-dir", str(jobs or dataset.parent / "jobs"),
        "--config-dir", str(configs or dataset.parent / "configs"),
        "--host-ip", "10.0.0.1",
    ]
    if resume is not None:
        argv += ["--resume-from", str(resume)]
    if not prewarm:
        argv.append("--no-prewarm")
    return [*argv, *extra]


@pytest.fixture
def stubbed_main(monkeypatch):
    """把 main() 会碰到的宿主世界全换成假的，只留下「选题 + 续跑剔除」这段真代码。

    `run_arm` 被换成记录器：这一组测试要钉的是 main() **交给**它的题目列表，
    不是 harbor 怎么跑（那是 run_arm 自己那几条测试的事）。
    """
    calls: list[dict] = []

    def fake_run_arm(**kwargs):
        calls.append(kwargs)
        return 0

    monkeypatch.setattr(runner, "_resolve_credentials", lambda model: ("https://gw.test/v1", "k"))
    monkeypatch.setattr(runner, "mycode_settings_env", lambda model, thinking: {})
    monkeypatch.setattr(runner, "run_arm", fake_run_arm)
    monkeypatch.setattr(runner, "detect_host_ip", lambda: "10.0.0.1")
    monkeypatch.setattr(runner, "needs_docker_group", lambda: False)
    return calls


def test_resume_skips_completed_tasks_and_keeps_the_selection_order(
    stubbed_main, tmp_path, capsys
):
    ids = _ten_ids()
    dataset = _make_dataset(tmp_path, ids)
    selected = select_tasks(ids, stage_n_tasks("full", len(ids)), SEED)
    assert len(selected) == 10

    resume = tmp_path / "old-job"
    # 故意挑不连续的三个位置（0 / 3 / 7）：连着挑就测不出「剩下的顺序有没有被打乱」
    done = [selected[0], selected[3], selected[7]]
    for index, name in enumerate(done):
        _write_trial(resume, f"{name}__{index}", name, reward=1.0)

    assert runner.main(_argv(dataset, resume)) == 0

    assert len(stubbed_main) == 1
    remaining = stubbed_main[0]["tasks"]
    # 顺序要紧：select_tasks 是按语言均衡轮转排的，剔除只能"抽掉"、不能重排，
    # 重排就破坏了分层（也破坏了"前 n 项"那个嵌套前提）
    assert remaining == [name for name in selected if name not in set(done)]
    assert len(remaining) == 7
    out = capsys.readouterr().out
    assert "跳过已完成的 3 题" in out
    assert "还要跑 7 题" in out


def test_resume_dir_with_a_task_outside_this_selection_is_refused(
    stubbed_main, monkeypatch, tmp_path
):
    """本轮最重要的一条：不许静默把两个总体合进一份报告。

    续跑目录里有一道本次没选中的题，就说明 seed / stage / 数据集变了。
    这时候照跑不误的话，合并出来的报告是一个混了两个总体的东西——而那正是
    变量守卫要防的那类错，只不过它发生在**数据采集**阶段，等报告出来已经查不出来了
    （`meta.task_ids` 只能告诉你两边题目集合不同，说不出是哪一次跑混进来的）。
    """
    ids = _ten_ids()
    dataset = _make_dataset(tmp_path, ids)
    probe = select_tasks(ids, stage_n_tasks("probe", len(ids)), SEED)
    assert len(probe) == 6
    outside = next(name for name in ids if name not in set(probe))

    resume = tmp_path / "old-job"
    _write_trial(resume, f"{probe[0]}__1", probe[0], reward=1.0)
    _write_trial(resume, f"{outside}__1", outside, reward=1.0)

    # 解析凭据换成炸弹：这条校验必须排在"跑任何东西"之前（解析凭据 / 预热 / 起 harbor），
    # 炸了就说明校验来晚了——而它来得早不早，是真跑批时几十分钟与几小时的差别
    def boom(model):
        raise AssertionError("foreign 校验没有排在解析凭据之前")

    monkeypatch.setattr(runner, "_resolve_credentials", boom)
    monkeypatch.setattr(runner, "run_arm", lambda **kwargs: pytest.fail("不该跑到 run_arm"))

    with pytest.raises(SystemExit) as excinfo:
        runner.main(_argv(dataset, resume, stage="probe"))

    message = str(excinfo.value)
    # 报错要说清是哪几道、以及最可能的原因，否则用户只会去重跑一遍 8.5 小时
    assert outside in message
    assert "不在本次选择范围内" in message
    assert "seed" in message and "--stage" in message and "数据集变了吗" in message
    assert stubbed_main == []  # 一道题都没交给 run_arm


def test_explicit_tasks_are_pruned_and_guarded_the_same_way(stubbed_main, tmp_path):
    """`--tasks` 那条路也走同一套剔除与校验——它不是绕过守卫的后门。

    顺带把一条**语义边界**钉在这里：`--resume-from` 的意思是「接着同一批题往下跑」，
    所以它要求续跑目录里的题 ⊆ 本次选择。想重跑其中某几道再合并，得走
    「`--tasks X,Y`（不带 --resume-from）跑出新目录 → `--run-dir 旧 --run-dir 新` 合并」
    这条既有路径：守卫没法把「我就是要重跑这两道」和「seed 变了」区分开，
    而这两种情况合并出来的报告含义完全不同，只能一律拒绝。
    """
    ids = _ten_ids()
    dataset = _make_dataset(tmp_path, ids)
    resume = tmp_path / "old-job"
    for index, name in enumerate(ids[:3]):
        _write_trial(resume, f"{name}__{index}", name, reward=1.0)

    # 点名 5 道、其中前 3 道已经跑完 → 只跑剩下 2 道，顺序按点名的顺序
    assert runner.main(_argv(dataset, resume, extra=("--tasks", ",".join(ids[0:5])))) == 0
    assert stubbed_main[0]["tasks"] == ids[3:5]

    # 点名的题里一道都不在续跑目录里 → 续跑目录整个成了"范围之外"，必须拒绝
    with pytest.raises(SystemExit) as excinfo:
        runner.main(_argv(dataset, resume, extra=("--tasks", ",".join(ids[6:8]))))
    assert "不在本次选择范围内" in str(excinfo.value)
    assert len(stubbed_main) == 1


def test_resume_from_rejects_arm_both(tmp_path):
    resume = tmp_path / "old-job"
    _write_trial(resume, "polyglot_cpp_alpha__1", "polyglot_cpp_alpha", reward=1.0)

    with pytest.raises(SystemExit) as excinfo:
        # 数据集目录**故意不存在**：这条校验必须排在读数据集之前，
        # 排晚了抛出来的就是 FileNotFoundError、报不出真正的原因
        runner.main(
            ["--arm", "both", "--resume-from", str(resume), "--dataset-dir", str(tmp_path / "nope")]
        )

    assert "只能配单个 --arm" in str(excinfo.value)


def test_resume_from_rejects_a_directory_that_is_not_a_job_dir(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    # 有 trial 子目录、但子目录里没有 result.json：harbor 被 kill 在半路就是这个形状。
    # 这种目录**必须**拒收：收下的话剔除集是空的，用户以为在续跑、其实又从零跑了 8.5 小时
    halfbaked = tmp_path / "halfbaked"
    (halfbaked / "polyglot_cpp_alpha__1").mkdir(parents=True)

    messages: list[str] = []
    for bad in (tmp_path / "missing", empty, halfbaked):
        with pytest.raises(SystemExit) as excinfo:
            runner.main(
                ["--arm", "opencode", "--resume-from", str(bad), "--dataset-dir", str(tmp_path / "nope")]
            )
        messages.append(str(excinfo.value))
        assert str(bad) in messages[-1]
        assert "不像是一个 harbor job 目录（没有 result.json）" in messages[-1]

    # 措辞与 --run-dir 那条**逐字一致**：同一个错在两条路上长两个样，
    # 用户就会以为是两回事
    with pytest.raises(SystemExit) as excinfo:
        runner.main(
            ["--arm", "opencode", "--run-dir", str(empty), "--dataset-dir", str(tmp_path / "nope")]
        )
    assert str(excinfo.value) == messages[1]


@pytest.fixture
def resumed_run(monkeypatch, tmp_path):
    """真跑一次「续跑」的 run_arm：续跑目录里已有 cpp_x / java_z，这次补跑 cpp_x + go_y。

    harbor 是 shell 出去的，所以这里把 runner 里的 `subprocess` 换成一个会**造出新 job
    目录**的假实现（形状照真的抄：目录名是时间戳、顶层有 result.json（`latest_run_dir`
    认的是它）、每道已完成的题一个子目录带 result.json（`collect_trials` 认的是它））。
    `polyglot_cpp_x` 故意在两边都有、而且 reward 不同：合并顺序错了就会取到旧的 0.0。
    （走 main() 造不出这个重叠——剔除之后两边必然不相交；但 run_arm 自己的契约是
    "新跑的目录放后面"，这条契约得有测试钉住，见 M-B。）
    """
    reports = tmp_path / "reports"
    monkeypatch.setattr(runner, "REPORTS_DIR", reports)
    monkeypatch.setattr(runner, "git_revision", lambda: "abc1234")
    monkeypatch.setattr(runner, "needs_docker_group", lambda: False)

    resume_dir = tmp_path / "old-job"
    _write_trial(resume_dir, "polyglot_cpp_x__1", "polyglot_cpp_x", reward=0.0)
    _write_trial(resume_dir, "polyglot_java_z__1", "polyglot_java_z", reward=1.0)

    jobs_dir = tmp_path / "jobs"
    new_dir = jobs_dir / "2026-10-19__10-00-00"
    shelled: list[list[str]] = []

    def fake_run(command, **kwargs):
        shelled.append([str(part) for part in command])
        new_dir.mkdir(parents=True, exist_ok=True)
        (new_dir / "result.json").write_text("{}", encoding="utf-8")
        _write_trial(new_dir, "polyglot_cpp_x__2", "polyglot_cpp_x", reward=1.0)
        _write_trial(new_dir, "polyglot_go_y__2", "polyglot_go_y", reward=0.0)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(runner, "subprocess", SimpleNamespace(run=fake_run))

    args = runner.build_parser().parse_args(
        _argv(tmp_path / "dataset", resume_dir, jobs=jobs_dir, configs=tmp_path / "configs")
    )
    returncode = runner.run_arm(
        arm="opencode",
        tasks=["polyglot_cpp_x", "polyglot_go_y"],
        args=args,
        base_url="https://gw.test/v1",
        api_key="k",
        settings_env=None,
    )
    produced = sorted(reports.glob("harbor_opencode_full_*.json"))
    assert len(produced) == 1, produced
    report = json.loads(produced[0].read_text(encoding="utf-8"))
    return SimpleNamespace(
        returncode=returncode,
        report=report,
        report_path=produced[0],
        resume_dir=resume_dir,
        new_dir=new_dir,
        shelled=shelled,
    )


def test_run_arm_merges_the_new_run_dir_last_so_the_rerun_wins(resumed_run):
    rows = {row["task_id"]: row for row in resumed_run.report["results"]}

    assert resumed_run.returncode == 0
    # 同名题以**新跑的**为准：旧目录那份是 0.0，新目录那份是 1.0
    assert rows["polyglot_cpp_x"]["reward"] == 1.0
    assert rows["polyglot_go_y"]["reward"] == 0.0
    # 续跑目录里没被重跑的那一道要活着进来，不能被新目录"挤掉"
    assert rows["polyglot_java_z"]["reward"] == 1.0
    assert len(resumed_run.report["results"]) == 3

    summary = resumed_run.report["summary"]
    assert summary["resumed_from"] == str(resumed_run.resume_dir)
    assert summary["n_resumed"] == 2
    # 两个目录都要看得见（旧的在前、新的在后），否则这份报告是从哪来的就查不到了
    assert summary["run_dir"] == f"{resumed_run.resume_dir}, {resumed_run.new_dir}"


def test_the_report_covers_the_merged_set_not_just_the_rerun_tasks(resumed_run):
    """`write_report(tasks=)` 给的是**合并后的全集**，不是这次补跑的那几道。

    核出来的理由：`meta.task_ids` 是对比层的一项控制变量——
    `eval/common/compare.py:variable_mismatches` 会比两臂的 task_ids 集合，
    不等就报 "meta.task_ids 未对齐"、守卫开火、headline 拒绝给结论。
    MyCode 臂那份已落盘的满跑报告记的是全部 225 题，所以续跑出来的 opencode 臂
    只记补跑的那 223 道，两臂就永远比不了了。这也和 `reprocess_run_dir` 的口径一致
    （它给的就是合并后所有 trial 的 task_name）。
    """
    meta = resumed_run.report["meta"]

    # run_arm 收到的 tasks 只有 2 道，报告覆盖的必须是 3 道
    assert meta["task_ids"] == ["polyglot_cpp_x", "polyglot_go_y", "polyglot_java_z"]
    assert resumed_run.report["summary"]["tasks"] == meta["task_ids"]
    assert len(meta["task_ids"]) == len(resumed_run.report["results"])


def test_resume_traces_never_reach_the_control_variables(resumed_run):
    """续跑痕迹记进 `summary`、**不许**记进 `variables`（硬性要求）。

    `variable_mismatches` 按两边键的**并集**比 variables：MyCode 臂那份报告记了 8 个
    控制变量，续跑的这一臂只要多记一个键，两臂对比就会报"变量未对齐（1 项不同）"、
    守卫开火、headline 拒绝给结论。而"续跑过"是运行元数据、不是控制变量：
    它不影响 agent 的行为，只影响数据是怎么采集到的。
    """
    variables = resumed_run.report["meta"]["variables"]

    # 就是 run_variables() 那 8 个键，一个都不许多
    assert set(variables) == {
        "model", "provider", "seed", "stage", "thinking_mode",
        "thinking_effective", "mycode_env", "git_revision",
    }
    assert not [key for key in variables if "resum" in key.lower()]
    # 痕迹在 summary 里（run_arm 那条测试已经断过值，这里断位置）
    assert "resumed_from" in resumed_run.report["summary"]
    assert "n_resumed" in resumed_run.report["summary"]


def test_a_resumed_report_still_compares_clean_against_a_plain_one(monkeypatch, tmp_path):
    """把上面那条要求推到守卫那一层：续跑的报告和没续跑的报告比，变量必须对齐。

    只断"键集相同"是不够的——守卫读的是报告，那就拿两份真报告去问守卫。
    """
    from eval.common.compare import variable_mismatches
    from eval.common.schema import check_report

    reports = tmp_path / "reports"
    monkeypatch.setattr(runner, "REPORTS_DIR", reports)

    trials_dir = tmp_path / "job"
    _write_trial(trials_dir, "polyglot_cpp_x__1", "polyglot_cpp_x", reward=1.0)
    trials = merge_trials([trials_dir])
    variables = {
        "model": "qwen3.6-27b", "provider": "openrouter", "seed": SEED, "stage": "full",
        "thinking_mode": "default", "thinking_effective": "跟随模型默认（未注入）",
        "mycode_env": {}, "git_revision": "abc1234",
    }
    common = dict(
        arm="opencode", stage="full", model="qwen3.6-27b", trials=trials,
        tasks=["polyglot_cpp_x"], seed=SEED, variables=variables,
    )
    plain = write_report(run_dirs=[trials_dir], stamp="20261019__000001", **common)
    resumed = write_report(
        run_dirs=[trials_dir, tmp_path / "new"],
        stamp="20261019__000002",
        resumed_from=trials_dir,
        n_resumed=1,
        **common,
    )

    plain_report = json.loads(plain.read_text(encoding="utf-8"))
    resumed_report = json.loads(resumed.read_text(encoding="utf-8"))

    assert resumed_report["summary"]["resumed_from"] == str(trials_dir)
    assert resumed_report["summary"]["n_resumed"] == 1
    # 没续跑的报告也带这两个键（None / 0）：读报告的人要能分清"没续跑"
    # 和"这份报告是加这两个键之前跑的"，缺键表达不了前者
    assert plain_report["summary"]["resumed_from"] is None
    assert plain_report["summary"]["n_resumed"] == 0

    assert set(resumed_report["meta"]["variables"]) == set(plain_report["meta"]["variables"])
    assert check_report(resumed_report) == []
    # 这一条是硬要求 (a) 的判据：守卫看不出这两份报告在控制变量上有任何差别
    assert variable_mismatches(plain_report, resumed_report) == []


def test_when_everything_is_done_no_harbor_is_started(monkeypatch, tmp_path, capsys):
    """题目全都跑完时：一个 harbor 进程都不起，只把续跑目录合并出报告。

    0 题的 job 是未定义行为——`build_job_config` 对空 tasks 直接 ValueError，
    所以这条路径连配置都不写。断言"没起 harbor"用的是 subprocess 记录器，
    不是"没崩就算过"。
    """
    ids = _ten_ids()[:4]
    dataset = _make_dataset(tmp_path, ids)
    resume = tmp_path / "old-job"
    for index, name in enumerate(ids):
        _write_trial(resume, f"{name}__{index}", name, reward=1.0 if index % 2 else 0.0)

    reports = tmp_path / "reports"
    configs = tmp_path / "configs"
    monkeypatch.setattr(runner, "REPORTS_DIR", reports)
    monkeypatch.setattr(runner, "git_revision", lambda: "abc1234")
    monkeypatch.setattr(runner, "_resolve_credentials", lambda model: ("https://gw.test/v1", "k"))
    monkeypatch.setattr(runner, "mycode_settings_env", lambda model, thinking: {})
    monkeypatch.setattr(runner, "needs_docker_group", lambda: False)

    shelled: list[list[str]] = []

    def fake_run(command, **kwargs):
        shelled.append([str(part) for part in command])
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(runner, "subprocess", SimpleNamespace(run=fake_run))

    assert runner.main(_argv(dataset, resume, configs=configs)) == 0

    assert shelled == []  # 没起 harbor、没探宿主 IP、没查 docker 组
    assert list(configs.glob("*.json")) == []  # 连 job 配置都没写
    out = capsys.readouterr().out
    assert "所有题目都已完成" in out

    produced = sorted(reports.glob("harbor_opencode_full_*.json"))
    assert len(produced) == 1
    report = json.loads(produced[0].read_text(encoding="utf-8"))
    # 报告是**全集**：4 道题都在，通过率按合并后的算（2 过 / 4 判分）
    assert sorted(report["meta"]["task_ids"]) == sorted(ids)
    assert report["summary"]["n_trials"] == 4
    assert report["summary"]["n_passed"] == 2
    assert report["summary"]["n_resumed"] == 4
    assert report["summary"]["resumed_from"] == str(resume)
    assert report["summary"]["run_dir"] == str(resume)
    assert report["summary"]["config_path"] is None


def test_prewarm_only_sees_the_tasks_that_still_have_to_run(stubbed_main, monkeypatch, tmp_path):
    """预热按剔除**之后**的列表做：已经跑完的题不用再等它构建镜像。

    go/rust 题的镜像构建要走代理，一次预热几十分钟；顺序反了（先预热再剔除）
    不会报错，只会白等——所以这条得钉住。
    """
    ids = _ten_ids()
    dataset = _make_dataset(tmp_path, ids)
    selected = select_tasks(ids, stage_n_tasks("full", len(ids)), SEED)
    resume = tmp_path / "old-job"
    done = [selected[2], selected[5]]
    for index, name in enumerate(done):
        _write_trial(resume, f"{name}__{index}", name, reward=1.0)

    prewarmed: list[str] = []

    def fake_prewarm(**kwargs):
        prewarmed.extend(kwargs["tasks"])
        return []

    monkeypatch.setattr(runner, "prewarm_images", fake_prewarm)

    assert runner.main(_argv(dataset, resume, prewarm=True)) == 0

    assert prewarmed == [name for name in selected if name not in set(done)]
    assert len(prewarmed) == 8
    # 交给 harbor 的和交给预热的必须是同一份剔除后的列表
    assert stubbed_main[0]["tasks"] == prewarmed


def test_completed_task_names_counts_failures_and_unscored_trials_as_done(tmp_path):
    """有 result.json 就算完成——**不管** pass / fail / 未评分。

    重跑失败的题等于挑数据：一次失败的测量也是一次测量。真的残留目录就是这个形状
    （opencode 臂那两道 cpp 都是 RewardFileNotFoundError → 按政策记 reward 0.0），
    如果只把"过了的"当完成，续跑就会重跑所有做错的题、把通过率刷上去。
    """
    job = tmp_path / "job"
    _write_trial(job, "passed__1", "polyglot_cpp_pass", reward=1.0)
    _write_trial(job, "failed__1", "polyglot_cpp_fail", reward=0.0)
    _write_trial(job, "errored__1", "polyglot_cpp_errored", reward=None)
    # 没有 result.json 的 trial 目录不算完成（harbor 被 kill 在半路就是这个形状），
    # 下一轮要重跑它
    (job / "running__1").mkdir(parents=True)
    # result.json 里没有 task_name：连题目都对不上，不能拿它去剔除任何一道
    _write_trial(job, "nameless__1", None, reward=1.0)

    assert completed_task_names(job) == {
        "polyglot_cpp_pass",
        "polyglot_cpp_fail",
        "polyglot_cpp_errored",
    }


def test_resume_of_an_empty_dir_is_refused_even_though_nothing_would_be_skipped(tmp_path):
    """`completed_task_names` 是空的 ≠ 可以续跑：那种目录根本不是 job 目录。

    和上面那条校验是一体两面——这里钉的是"空集合"不会被当成"全部未完成、照跑"。
    """
    job = tmp_path / "job"
    job.mkdir()

    assert completed_task_names(job) == set()
    with pytest.raises(SystemExit):
        runner.main(
            ["--arm", "opencode", "--resume-from", str(job), "--dataset-dir", str(tmp_path / "nope")]
        )
