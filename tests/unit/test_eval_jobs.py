"""执行层的记录与恢复：状态错了不会报错，只会让页面永远显示"还在跑"。"""

from __future__ import annotations

import json
import os

import pytest

from eval.common.jobs import (
    JobRecord,
    JobStatus,
    attach_on_startup,
    events_path,
    get_job,
    job_path,
    jobs_dir,
    list_jobs,
    pid_alive,
    running_jobs,
    write_job,
)


def _record(run_id="gaia-20261018_000000-abc123", **over) -> JobRecord:
    base = dict(
        run_id=run_id,
        benchmark="gaia",
        argv=[".venv/bin/python", "-m", "eval.gaia.runner"],
        params={"sample": 5},
        git_revision="abc1234",
        status=JobStatus.RUNNING,
        pid=os.getpid(),
        started_at="2026-10-18T00:00:00+08:00",
        finished_at=None,
        exit_code=None,
        report_path=None,
        error=None,
    )
    base.update(over)
    return JobRecord(**base)


@pytest.fixture
def reports(tmp_path, monkeypatch):
    monkeypatch.setattr("eval.common.jobs.REPORTS_DIR", tmp_path)
    return tmp_path


def test_paths_live_under_reports_dir(reports):
    assert jobs_dir() == reports / "jobs"
    assert job_path("r1") == reports / "jobs" / "r1.job.json"
    assert events_path("r1") == reports / "r1.events.jsonl"


def test_write_then_get_roundtrips(reports):
    rec = _record()

    write_job(rec)

    assert get_job(rec.run_id) == rec


def test_write_job_creates_the_jobs_dir_and_is_atomic(reports):
    rec = _record()

    path = write_job(rec)

    assert path.exists()
    # 原子写不能留下临时文件
    assert sorted(p.name for p in (reports / "jobs").iterdir()) == [f"{rec.run_id}.job.json"]
    assert json.loads(path.read_text(encoding="utf-8"))["status"] == "running"


def test_get_job_returns_none_for_unknown(reports):
    assert get_job("nope") is None


def test_record_survives_dict_roundtrip():
    rec = _record(status=JobStatus.COMPLETED, exit_code=0, finished_at="2026-10-18T01:00:00+08:00")

    assert JobRecord.from_dict(rec.to_dict()) == rec


def test_list_jobs_merges_legacy_state_files(reports):
    # 旧的 gaia/hle/smoke run 只有 .state.json，不合并进来历史就凭空少一截
    write_job(_record(run_id="new-1", started_at="2026-10-18T02:00:00+08:00"))
    (reports / "old-1.state.json").write_text(
        json.dumps(
            {
                "run_id": "old-1",
                "benchmark": "hle",
                "status": "completed",
                "created_at": "2026-10-01T00:00:00+08:00",
                "started_at": "2026-10-01T00:00:00+08:00",
                "summary": {"total": 3, "correct": 2},
            }
        ),
        encoding="utf-8",
    )

    runs = list_jobs()

    assert [r.run_id for r in runs] == ["new-1", "old-1"]  # 按 started_at 倒序
    assert runs[1].benchmark == "hle"
    assert runs[1].status == JobStatus.COMPLETED


def test_list_jobs_prefers_job_file_over_state_for_same_run_id(reports):
    write_job(_record(run_id="dup", status=JobStatus.FAILED, error="boom"))
    (reports / "dup.state.json").write_text(
        json.dumps({"run_id": "dup", "benchmark": "gaia", "status": "running",
                    "started_at": "2026-10-18T00:00:00+08:00"}),
        encoding="utf-8",
    )

    runs = list_jobs()

    assert len(runs) == 1
    assert runs[0].status == JobStatus.FAILED


def test_list_jobs_skips_unreadable_files(reports):
    write_job(_record(run_id="ok"))
    (reports / "bad.state.json").write_text("{ not json", encoding="utf-8")

    assert [r.run_id for r in list_jobs()] == ["ok"]


def test_list_jobs_respects_limit(reports):
    for i in range(5):
        write_job(_record(run_id=f"r{i}", started_at=f"2026-10-1{i}T00:00:00+08:00"))

    assert len(list_jobs(limit=2)) == 2


def test_pid_alive_true_for_self():
    assert pid_alive(os.getpid()) is True


def test_pid_alive_false_for_none_and_dead_pids():
    assert pid_alive(None) is False
    assert pid_alive(2_000_000_000) is False


def test_attach_on_startup_marks_dead_runs_interrupted(reports):
    dead = _record(run_id="dead", pid=2_000_000_000, status=JobStatus.RUNNING)
    alive = _record(run_id="alive", pid=os.getpid(), status=JobStatus.RUNNING)
    done = _record(run_id="done", pid=2_000_000_000, status=JobStatus.COMPLETED)
    for rec in (dead, alive, done):
        write_job(rec)

    fixed = attach_on_startup()

    assert fixed == ["dead"]
    assert get_job("dead").status == JobStatus.INTERRUPTED
    # 还活着的不能动，已结束的不能动
    assert get_job("alive").status == JobStatus.RUNNING
    assert get_job("done").status == JobStatus.COMPLETED


def test_attach_on_startup_on_empty_dir(reports):
    assert attach_on_startup() == []


def test_running_jobs_only_counts_queued_and_running(reports):
    write_job(_record(run_id="a", status=JobStatus.RUNNING))
    write_job(_record(run_id="b", status=JobStatus.QUEUED))
    write_job(_record(run_id="c", status=JobStatus.COMPLETED))
    write_job(_record(run_id="d", status=JobStatus.INTERRUPTED))

    assert sorted(r.run_id for r in running_jobs()) == ["a", "b"]


# ── 旧 .state.json 的真实形状 ─────────────────────────────────────────
# 真盘上的 state 文件是 EvalRunState.to_dict() 写的：时间戳是 time.time() 的
# **float**，状态词表是 RunStatus（有 pending、没有 queued/interrupted）。
# 上面那些手写成 ISO 字符串的 fixture 长得好看，但和盘上的数据不是一回事——
# 类型混进同一个 sorted() 会 TypeError，而 list_jobs 一炸，
# 运行列表和 MAX_CONCURRENT_JOBS 闸门会一起没。

LEGACY_EPOCH = 1790892113.65  # 2026-10-01 前后，取自真盘上的一份 state 文件


def _write_legacy(reports, run_id, **over):
    data = {"run_id": run_id, "benchmark": "gaia", "status": "completed",
            "created_at": LEGACY_EPOCH, "started_at": LEGACY_EPOCH,
            "finished_at": LEGACY_EPOCH + 900.0,
            "report_json_path": f"/x/{run_id}.json"}
    data.update(over)
    (reports / f"{run_id}.state.json").write_text(json.dumps(data), encoding="utf-8")


def test_legacy_float_timestamps_sort_against_new_iso_ones(reports):
    _write_legacy(reports, "old")
    write_job(_record(run_id="new", started_at="2026-10-18T02:00:00+08:00"))

    runs = list_jobs()  # 不抛 TypeError

    assert [r.run_id for r in runs] == ["new", "old"]  # 新的在前
    old = runs[1]
    assert isinstance(old.started_at, str) and old.started_at.startswith("2026-")
    assert isinstance(old.finished_at, str)


def test_legacy_pending_maps_to_queued_not_failed(reports):
    # service.py 在 create_state 之后就 save_state，所以盘上真有 status=pending 的文件；
    # 映射成 failed 会把"还没开始"报成"跑挂了"
    _write_legacy(reports, "p", status="pending", finished_at=None)

    assert list_jobs()[0].status == JobStatus.QUEUED


def test_a_legacy_record_with_an_unknown_status_is_interrupted_not_completed(reports):
    # 读不懂的记录报成"成功跑完"是往错的方向撒谎
    _write_legacy(reports, "q", status=None)

    assert list_jobs()[0].status == JobStatus.INTERRUPTED


def test_a_legacy_running_record_does_not_occupy_the_concurrency_slot(reports):
    # 旧路径（进程内 asyncio）留下的 running 没有 pid：查不了死活也杀不掉。
    # 算进名额的话，MAX_CONCURRENT_JOBS=1 会被一份陈年文件永久焊死
    _write_legacy(reports, "stuck", status="running", finished_at=None)

    assert running_jobs() == []
    # 列表里仍如实显示 running——不替它撒谎，也不拿它挡新任务
    assert list_jobs()[0].status == JobStatus.RUNNING


def test_attach_on_startup_closes_the_record_it_interrupts(reports):
    write_job(_record(run_id="dead", pid=2_000_000_000, status=JobStatus.RUNNING,
                      finished_at=None, error="boom"))

    attach_on_startup()

    fixed = get_job("dead")
    assert fixed.status == JobStatus.INTERRUPTED
    # 不收口的话这一条永远没有结束时间，页面上的耗时只能拿 now 去减，越看越长
    assert fixed.finished_at
    assert "boom" in fixed.error and "interrupted" in fixed.error
    assert "boom服务" not in fixed.error  # 两段之间要有分隔，不能粘成一个词


# ── Task 3：起进程与中止 ──────────────────────────────────────────────

import signal  # noqa: E402
import subprocess  # noqa: E402  （追加段，import 放这里避免改动文件头）
import sys  # noqa: E402
import time  # noqa: E402

from eval.common.jobs import (  # noqa: E402
    BENCHMARK_MODULES,
    JobStartError,
    _PROCS,
    abort_job,
    build_argv,
    finish_job,
    git_revision,
    reap_finished,
    start_job,
)


class _FakeProc:
    def __init__(self, pid=4321, exit_code=None):
        self.pid = pid
        # None = 还在跑。测试可以中途把它翻成 0，让这假进程"退出"
        self.exit_code = exit_code
        self.kwargs: dict = {}

    def poll(self):
        return self.exit_code  # 默认 None（还在跑），形状和真 Popen.poll() 一致


def _fake_popen(record: list):
    def popen(argv, **kwargs):
        record.append((argv, kwargs))
        return _FakeProc()

    return popen


def test_build_argv_maps_benchmark_to_its_module():
    argv = build_argv("harbor", {"arm": "mycode", "stage": "final"})

    assert argv[0].endswith("python") or "python" in argv[0]
    assert "-m" in argv and "eval.harbor.runner" in argv
    # 参数按 --key value 展开；None 值的键要跳过，否则 CLI 会收到字符串 "None"
    assert "--arm" in argv and argv[argv.index("--arm") + 1] == "mycode"


def test_build_argv_skips_none_and_expands_bool_flags():
    argv = build_argv("gaia", {"sample": 5, "judge": True, "category": None})

    assert "--sample" in argv and argv[argv.index("--sample") + 1] == "5"
    assert "--judge" in argv
    # True 只出 flag 不出值；None 整个跳过。
    # 不能写 argv[argv.index("--judge") + 1]：category 被跳过后 --judge 就是最后一个，
    # 取下标会 IndexError——而 IndexError 恰好把"没有值"这件事报成了错误
    assert "True" not in argv
    assert "--category" not in argv


def test_build_argv_drops_false_bool_flags():
    argv = build_argv("gaia", {"judge": False, "sample": 3})

    # False 的 store_true 开关只能整个不出：出了 flag 就等于在 CLI 那边翻成 True
    assert "--judge" not in argv
    assert "--sample" in argv


def test_build_argv_rejects_unknown_benchmark():
    with pytest.raises(JobStartError):
        build_argv("nope", {})


def test_benchmark_modules_covers_every_benchmark():
    assert set(BENCHMARK_MODULES) >= {"gaia", "hle", "smoke", "loca", "harbor"}


def test_start_job_writes_record_and_passes_event_env(reports):
    calls: list = []

    rec = start_job("gaia", {"sample": 2}, popen=_fake_popen(calls))

    assert rec.status == JobStatus.RUNNING
    assert rec.pid == 4321
    assert rec.benchmark == "gaia"
    assert get_job(rec.run_id) == rec
    argv, kwargs = calls[0]
    env = kwargs["env"]
    # 子进程靠这两个 env 找到自己该往哪写事件
    assert env["MYCODE_EVAL_RUN_ID"] == rec.run_id
    assert env["MYCODE_EVAL_EVENTS_PATH"] == str(events_path(rec.run_id))
    # 必须脱离服务的进程组，否则服务被杀时它跟着死
    assert kwargs["start_new_session"] is True


def test_start_job_records_git_revision(reports):
    rec = start_job("gaia", {}, popen=_fake_popen([]))

    assert rec.git_revision  # 非空；具体值取决于仓库状态
    assert git_revision() == rec.git_revision


def test_git_revision_returns_unknown_when_the_delegate_raises(monkeypatch):
    # 委托的 eval.harbor.runner.git_revision 只兜住了第一个 git 子进程，
    # `git status --porcelain` 那次的 TimeoutExpired 会抛出来。start_job 里它一抛，
    # 已经起来的评测子进程就成了没索引、不占名额、中止不掉的孤儿
    def boom():
        raise subprocess.TimeoutExpired(cmd="git", timeout=15)

    monkeypatch.setattr("eval.harbor.runner.git_revision", boom)

    assert git_revision() == "unknown"


def test_start_job_refuses_when_one_is_already_running(reports):
    start_job("gaia", {}, popen=_fake_popen([]))

    with pytest.raises(JobStartError):
        start_job("hle", {}, popen=_fake_popen([]))


def test_start_job_refuses_while_an_aborted_process_is_still_dying(reports, monkeypatch):
    rec = start_job("gaia", {}, popen=_fake_popen([]))
    # killer= 必须传假的：默认那条路会真的 os.killpg(4321)，打到机器上恰好存在的进程组
    abort_job(rec.run_id, killer=lambda pid, sig: None)

    # abort_job 只发 SIGTERM 就返回、不等进程真死，所以闸门得自己看一眼还活着没有；
    # 否则"中止 → 改参数 → 重跑"会放一轮新的进来，两轮抢同一个网关配额
    monkeypatch.setattr("eval.common.jobs.pid_alive", lambda pid: True)
    with pytest.raises(JobStartError) as exc:
        start_job("gaia", {}, popen=_fake_popen([]))
    assert "收尾" in str(exc.value)

    # 进程真死了就得照常放行——这道闸门不能把评测永久焊死
    monkeypatch.setattr("eval.common.jobs.pid_alive", lambda pid: False)
    assert start_job("gaia", {}, popen=_fake_popen([])).status == JobStatus.RUNNING


def test_start_job_allows_new_one_after_previous_finished(reports):
    first = start_job("gaia", {}, popen=_fake_popen([]))
    finish_job(first.run_id, 0)

    second = start_job("hle", {}, popen=_fake_popen([]))

    assert second.status == JobStatus.RUNNING


def test_start_job_kills_the_child_if_the_index_write_fails(reports, monkeypatch):
    calls: list = []
    killed: list = []

    def no_write(record, reports_dir=None):
        raise OSError("disk full")

    monkeypatch.setattr("eval.common.jobs.write_job", no_write)
    monkeypatch.setattr(
        "eval.common.jobs.os.killpg", lambda pgid, sig: killed.append((pgid, sig))
    )

    with pytest.raises(OSError, match="disk full"):
        start_job("gaia", {}, popen=_fake_popen(calls))

    # 子进程已经起来了、索引却没写上：不杀掉它就是一条查不到也中止不掉的孤儿
    assert killed == [(4321, signal.SIGTERM)]
    # run_id 从 fake spawn 看到的 env 取——write_job 被打了补丁，jobs 目录是空的，
    # list_jobs 那条路拿不到这条 run
    run_id = calls[0][1]["env"]["MYCODE_EVAL_RUN_ID"]
    assert get_job(run_id) is None
    # 索引没写上＝reap_finished 再也不会访问这个 run_id：名册里不摘掉，
    # 漏的就是句柄 + 它身后那只没人收的僵尸（_PROCS 还是模块级状态，会串到后面的测试）
    assert run_id not in _PROCS


def test_finish_job_maps_exit_code_to_status(reports):
    rec = start_job("gaia", {}, popen=_fake_popen([]))
    (reports / f"{rec.run_id}.json").write_text("{}", encoding="utf-8")

    finish_job(rec.run_id, 0)

    done = get_job(rec.run_id)
    assert done.status == JobStatus.COMPLETED
    assert done.exit_code == 0
    assert done.finished_at
    assert done.report_path == str(reports / f"{rec.run_id}.json")


def test_a_zero_exit_without_a_report_is_downgraded_to_failed(reports):
    # 退出码 0 却什么都没留下：页面会显示"完成"而详情页是空的，
    # 这比失败更难查，所以宁可报失败
    rec = start_job("gaia", {}, popen=_fake_popen([]))

    finish_job(rec.run_id, 0)

    done = get_job(rec.run_id)
    assert done.status == JobStatus.FAILED
    assert "报告未落盘" in (done.error or "")


def test_a_dry_run_is_allowed_to_produce_no_report(reports):
    # harbor 的 --dry-run / --install-only 本来就只校验配置、只装 agent，
    # 不写报告是**正确行为**，不能因此被判失败
    for params in ({"dry_run": True}, {"install_only": True}):
        rec = start_job("harbor", params, popen=_fake_popen([]))

        finish_job(rec.run_id, 0)

        assert get_job(rec.run_id).status == JobStatus.COMPLETED, params


def test_finish_job_marks_failure_and_keeps_stderr_tail(reports):
    rec = start_job("gaia", {}, popen=_fake_popen([]))

    finish_job(rec.run_id, -9)

    failed = get_job(rec.run_id)
    assert failed.status == JobStatus.FAILED
    assert failed.exit_code == -9
    assert "信号" in (failed.error or "")


def test_finish_job_on_unknown_run_is_none(reports):
    assert finish_job("nope", 0) is None


def test_abort_job_sends_sigterm_to_the_process_group(reports):
    sent: list = []
    rec = start_job("gaia", {}, popen=_fake_popen([]))

    out = abort_job(rec.run_id, killer=lambda pid, sig: sent.append((pid, sig)))

    # 打的是**进程组**（pgid == pid，因为 start_new_session=True 让子进程自成一组），
    # 这样 runner 起的 harbor/docker 子进程也会一起收信号，不会留下孤儿容器
    assert sent == [(rec.pid, signal.SIGTERM)]
    assert out.status == JobStatus.ABORTED


def test_abort_job_defaults_to_killing_the_process_group(reports, monkeypatch):
    # 上面那条注入了 killer=，所以它压根区分不出默认走的是 os.killpg 还是 os.kill。
    # 而这个默认值的差别在真机上是"连带收掉 runner 起的 harbor/docker"和"留下一堆孤儿容器"
    sent: list = []
    rec = start_job("gaia", {}, popen=_fake_popen([]))
    monkeypatch.setattr(
        "eval.common.jobs.os.killpg", lambda pgid, sig: sent.append((pgid, sig))
    )

    abort_job(rec.run_id)

    assert sent == [(rec.pid, signal.SIGTERM)]


def test_abort_job_on_finished_job_is_a_noop(reports):
    sent: list = []
    rec = start_job("gaia", {}, popen=_fake_popen([]))
    # 报告必须真存在：否则退出码 0 会被降级成 failed（见上面那条降级测试），
    # 而这条要的是"一个确实跑完了的任务"
    (reports / f"{rec.run_id}.json").write_text("{}", encoding="utf-8")
    finish_job(rec.run_id, 0)

    out = abort_job(rec.run_id, killer=lambda pid, sig: sent.append((pid, sig)))

    assert sent == []
    assert out.status == JobStatus.COMPLETED


def test_abort_job_unknown_run_returns_none(reports):
    assert abort_job("nope") is None


# ── Task 3 复审第 2 轮：reap_finished 收尸 ───────────────────────────
# start_job 起了子进程就丢掉 Popen 句柄，全仓没有任何地方 wait()：退出的子进程变成僵尸，
# 而 os.kill(pid, 0) 对僵尸是**成功**的——pid_alive 会一直说"还活着"。于是状态永远停在
# running，MAX_CONCURRENT_JOBS=1 的闸门永久关着，不重启服务就再也起不了第二轮评测。
# 一次 waitpid(WNOHANG) 同时解决判死、回收僵尸、拿真实退出码三件事。
#
# 所以下面这些测试必须起**真的**子进程：waitpid 只认自己的子进程，_FakeProc(pid=4321)
# 那条假路走不通（它只会命中 ChildProcessError 分支）。


def _cleanup_child(proc):
    """测试里起的真子进程必须收干净，断言失败也要收。

    漏一个会占着资源、让后面的测试变得不稳定（其中一条起的子进程要睡 30 秒）。
    kill 打不到已经退出的进程（send_signal 自己吞 ProcessLookupError），
    wait 对被 reap_finished 抢先收走的子进程也不抛（Popen 兜住了 ChildProcessError）。
    """
    try:
        proc.kill()
    except OSError:
        pass
    try:
        proc.wait(timeout=10)
    except (OSError, subprocess.SubprocessError):
        pass


def _poll_reap(reports, run_id, timeout=5.0):
    """轮询到 reap_finished 收下这条为止，返回最后一次的收尸列表。

    不能图省事 proc.wait() 再调 reap_finished：wait() 已经把僵尸收走了，
    被测的那次 waitpid 只会抛 ChildProcessError 走 continue——测的是假路径。
    """
    deadline = time.monotonic() + timeout
    reaped: list[str] = []
    while time.monotonic() < deadline:
        reaped = reap_finished(reports_dir=reports)
        if run_id in reaped:
            break
        time.sleep(0.05)
    return reaped


def test_reap_finished_completes_a_job_whose_process_exited(reports):
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    try:
        run_id = "done"
        # 报告得真存在：否则退出码 0 会被 finish_job 降级成 failed（见上面那条降级测试）
        (reports / f"{run_id}.json").write_text("{}", encoding="utf-8")
        write_job(_record(run_id=run_id, status=JobStatus.RUNNING, pid=proc.pid))

        reaped = _poll_reap(reports, run_id)

        assert run_id in reaped
        done = get_job(run_id)
        assert done.status is JobStatus.COMPLETED
        assert done.exit_code == 0
    finally:
        _cleanup_child(proc)


def test_reap_finished_records_a_signal_death_as_a_negative_exit_code(reports):
    proc = subprocess.Popen(
        [sys.executable, "-c", "import os, signal; os.kill(os.getpid(), signal.SIGTERM)"]
    )
    try:
        run_id = "sig"
        write_job(_record(run_id=run_id, status=JobStatus.RUNNING, pid=proc.pid))

        reaped = _poll_reap(reports, run_id)

        assert run_id in reaped
        failed = get_job(run_id)
        assert failed.status is JobStatus.FAILED
        # waitstatus_to_exitcode 把信号死报成负数——finish_job 那句"被信号 N 杀掉"
        # 全靠这个约定，符号一翻就变成"被信号 -15 杀掉"
        assert failed.exit_code == -signal.SIGTERM
    finally:
        _cleanup_child(proc)


def test_reap_finished_leaves_a_running_child_alone(reports):
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        run_id = "running"
        write_job(_record(run_id=run_id, status=JobStatus.RUNNING, pid=proc.pid))

        assert reap_finished(reports_dir=reports) == []
        assert get_job(run_id).status is JobStatus.RUNNING
    finally:
        # 不收掉就漏一个要睡 30 秒的进程在这儿
        _cleanup_child(proc)


def test_reap_finished_skips_a_pid_that_is_not_our_child(reports):
    # 服务重启过、或 pid 被别的进程复用了：waitpid 抛 ChildProcessError。
    # 不兜住它，reap_finished 会在每一条重启前留下的记录上炸——而 start_job 现在
    # 一进门就调它，等于任何一次启动都直接 500
    write_job(_record(run_id="foreign", status=JobStatus.RUNNING, pid=1))

    assert reap_finished(reports_dir=reports) == []

    # 判不了死活就一个字都别改，那是 attach_on_startup 的活
    assert get_job("foreign").status is JobStatus.RUNNING


def test_start_job_reaps_a_finished_job_before_checking_the_gate(reports):
    # 这条钉的是"焊死"本身：上一轮已经退出、状态却还挂着 running（没人 wait 过它），
    # running_jobs 就一直算它一个名额，闸门永久关着，不重启服务再也起不了第二轮
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    try:
        run_id = "wedge"
        (reports / f"{run_id}.json").write_text("{}", encoding="utf-8")
        write_job(_record(run_id=run_id, status=JobStatus.RUNNING, pid=proc.pid))

        started = None
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            try:
                started = start_job("gaia", {}, reports_dir=reports, popen=_fake_popen([]))
                break
            except JobStartError:
                time.sleep(0.05)  # 子进程还没退出，这时候拒得对；等它退出并被收掉

        assert started is not None and started.status is JobStatus.RUNNING
        # 放行是**因为**先把上一条收了，不是绕过了闸门
        assert get_job(run_id).status is JobStatus.COMPLETED
    finally:
        _cleanup_child(proc)


# ── Task 3 复审第 3 轮：句柄必须持着，ABORTED 也要收 ─────────────────
# 两个洞，同一个后果：MAX_CONCURRENT_JOBS=1 的闸门被焊死，不重启后端再也起不了第二轮。
#
# (1) start_job 只留 proc.pid、让 Popen 对象引用计数归零，Popen.__del__ 就把它挂到
#     subprocess._active 上；此后这个进程里**任何**一次 Popen 构造都先跑 subprocess._cleanup()，
#     用 waitpid(WNOHANG) 把它收走。后端是一个 uvicorn 进程同时服务评测 API 和 agent/git 路由，
#     普通只读端点就会构造 Popen（GET /api/projects/{cwd}/worktrees → worktree._run_git →
#     subprocess.run）。它抢在 reap_finished 前面收尸，我们那次 waitpid 只剩 ChildProcessError，
#     记录永远停在 running。所以 _PROCS 必须持着句柄，收尸走 proc.poll()。
# (2) abort_job 只发 SIGTERM 就把状态写成 ABORTED，进程随后退出照样变僵尸，而 os.kill(pid, 0)
#     对僵尸是成功的：不收它，start_job 的 dying 闸门就永远当它"还在收尾"，同时 finish_job
#     里那条专门给 ABORTED 的分支（只补退出码、不改状态）永远没人调。
#
# 与第 2 轮那组相反，这组测试**大多不需要真子进程**：句柄在手上时走的是 poll()，
# 一个能翻 exit_code 的假进程就是诚实的双身。唯一的例外是收 ABORTED 那条，它要钉住
# "没有句柄（服务重启过）时的 waitpid 退路"照样收得到，所以起真子进程。


def _controllable_popen():
    """假 popen + 一份名册，名册里是 (run_id, 假进程)。

    _fake_popen 把造出来的对象丢掉了（只记 argv/kwargs），而这组测试要"先让 poll() 返回
    None、再让它返回 0"，所以必须拿得到那个对象。run_id 从工厂看到的 env 里取——那正是
    start_job 拿去当 _PROCS 键的值，所以测试结束能照着名册把模块级状态清干净。
    """
    made: list = []

    def popen(argv, **kwargs):
        proc = _FakeProc()
        made.append((kwargs["env"]["MYCODE_EVAL_RUN_ID"], proc))
        return proc

    return popen, made


def _drop_handles(made):
    """_PROCS 是模块级状态：漏一条就串到后面的测试，所以每条都在 finally 里过这里。"""
    for run_id, _ in made:
        _PROCS.pop(run_id, None)


def test_reap_finished_uses_the_retained_handle_not_a_raw_waitpid(reports):
    popen, made = _controllable_popen()
    try:
        rec = start_job("gaia", {}, reports_dir=reports, popen=popen)
        _, fake = made[0]
        # 报告得真存在：否则退出码 0 会被 finish_job 降级成 failed（见上面那条降级测试）
        (reports / f"{rec.run_id}.json").write_text("{}", encoding="utf-8")

        # pid 是 4321，不是本进程的子进程：真去 waitpid 只会 ChildProcessError 走 continue。
        # 唯一收得到它的路，就是用持着的句柄 poll()
        assert fake.poll() is None
        assert reap_finished(reports_dir=reports) == []
        assert get_job(rec.run_id).status is JobStatus.RUNNING

        fake.exit_code = 0
        assert reap_finished(reports_dir=reports) == [rec.run_id]
        done = get_job(rec.run_id)
        assert done.status is JobStatus.COMPLETED
        assert done.exit_code == 0
    finally:
        _drop_handles(made)


def test_start_job_retains_the_popen_handle(reports):
    popen, made = _controllable_popen()
    try:
        rec = start_job("gaia", {}, reports_dir=reports, popen=popen)

        # 存进去的必须是**同一个对象**：不持着它，Popen.__del__ 就把它挂上 subprocess._active，
        # 此后随便一次 Popen 构造（worktrees 那种只读端点就够）都会先 _cleanup() 替我们收尸
        assert _PROCS[rec.run_id] is made[0][1]
        # 键就是 run_id：_poll_exit 全靠这个对应关系找句柄
        assert rec.run_id == made[0][0]
    finally:
        _drop_handles(made)


def test_reap_finished_drops_the_handle_once_reaped(reports):
    popen, made = _controllable_popen()
    try:
        rec = start_job("gaia", {}, reports_dir=reports, popen=popen)
        assert rec.run_id in _PROCS

        made[0][1].exit_code = 0
        reap_finished(reports_dir=reports)

        # 收完还留着就是泄漏：_PROCS 会跟着历史运行数一直长，而且留着的是已死的句柄
        assert rec.run_id not in _PROCS
    finally:
        _drop_handles(made)


def test_reap_finished_collects_an_aborted_job_and_records_its_exit_code(reports):
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    try:
        run_id = "aborted"
        # 手写记录（不经 start_job）＝句柄不在 _PROCS 里，正是服务重启过的那条退路
        write_job(_record(run_id=run_id, status=JobStatus.ABORTED, pid=proc.pid))

        reaped = _poll_reap(reports, run_id)

        assert run_id in reaped
        rec = get_job(run_id)
        # 状态必须**还是** ABORTED：finish_job 对中止过的运行只补退出码，不能升级成 completed。
        # 故意不写报告文件——那条早退分支在"退出码 0 却没报告就降级 failed"之前就返回了，
        # 少了它这条断言就区分不出"走了 ABORTED 分支"和"碰巧报告存在"
        assert rec.status is JobStatus.ABORTED
        assert rec.exit_code == 0
        assert rec.finished_at
    finally:
        _cleanup_child(proc)


def test_an_aborted_job_whose_process_died_reopens_the_gate(reports, monkeypatch):
    popen, made = _controllable_popen()
    reaped: list = []
    try:
        rec = start_job("gaia", {}, reports_dir=reports, popen=popen)
        fake = made[0][1]
        # killer= 必须传假的：默认那条路会真的 os.killpg(4321)，打到机器上恰好存在的进程组
        abort_job(rec.run_id, reports_dir=reports, killer=lambda pid, sig: None)
        # pid_alive 挂在"收过尸没有"上，而不是手翻的常量：真机上僵尸被收走之前
        # os.kill(pid, 0) 一直成功，收走之后 pid 才消失。手翻开关会让这条绿得不诚实——
        # 它必须**真的被 reap_finished 收下**才放行，否则就复现那个永久焊死
        monkeypatch.setattr("eval.common.jobs.pid_alive", lambda pid: rec.run_id not in reaped)

        # 进程还没死透：闸门报"还在收尾"，这时候拒得对
        assert fake.poll() is None
        with pytest.raises(JobStartError) as exc:
            start_job("gaia", {}, reports_dir=reports, popen=popen)
        assert "收尾" in str(exc.value)

        # 进程退了：ABORTED 也得收，不然僵尸永远在，上面那道闸门再也打不开
        fake.exit_code = 0
        reaped.extend(reap_finished(reports_dir=reports))
        assert reaped == [rec.run_id]

        second = start_job("gaia", {}, reports_dir=reports, popen=popen)
        assert second.status is JobStatus.RUNNING
        # 放行是**因为**僵尸被收了；而收尸只补退出码，不把中止过的运行改成 completed
        assert get_job(rec.run_id).status is JobStatus.ABORTED
    finally:
        _drop_handles(made)


# ── Task 6 复审第 1 轮：服务重启后留下的孤儿也要收 ─────────────────────
# _PROCS 是进程内的。后端重启时子进程还活着（detached，杀不到），重启后名册是空的，
# _poll_exit 只能退回 os.waitpid；而那个孩子早被 reparent 给 init、init 也早把它收走了，
# waitpid 只会 ChildProcessError → _poll_exit 返回 None。退出码这辈子都拿不到了，
# 但"进程没了"还判得出来。
#
# 不判的后果是**永久**的：finish_job 全仓只有 reap_finished 一个调用点，
# 记录于是一直停在 running，start_job 的闸门一直关着，要等到下一次重启才由
# attach_on_startup 打开——而那时它被标成 interrupted，哪怕它其实成功写了报告。
# Task 6 把 tailer 变成唯一的周期收尸者之后，这条路从"理论缺口"变成了"必然焊死"。
#
# 用 2_000_000_000 当 pid：它既不是本测试进程的孩子（waitpid → ChildProcessError），
# 也不存在（pid_max 是 4194304，见 test_pid_alive_false_for_none_and_dead_pids），
# 正好是"重启前留下的孤儿"的双身。


def test_reap_finished_finalizes_a_post_restart_orphan_whose_pid_is_gone(reports):
    run_id = "orphan"
    # 报告在：它真的跑完了，只是没人收尸。退出码判不了，就读 runner 自己写下的 status
    (reports / f"{run_id}.json").write_text("{}", encoding="utf-8")
    (reports / f"{run_id}.state.json").write_text(
        json.dumps({"run_id": run_id, "status": "completed"}), encoding="utf-8"
    )
    write_job(_record(run_id=run_id, status=JobStatus.RUNNING, pid=2_000_000_000))

    assert reap_finished(reports_dir=reports) == [run_id]

    done = get_job(run_id)
    # 不是 interrupted：成功写了报告的运行必须报成功，否则页面上是一次假失败
    assert done.status is JobStatus.COMPLETED
    assert done.exit_code == 0
    assert done.finished_at
    assert done.report_path == str(reports / f"{run_id}.json")
    # 收掉了就不该再占名额，闸门随之重开
    assert running_jobs(reports_dir=reports) == []


def test_reap_finished_records_a_post_restart_orphan_without_a_report_as_failed(reports):
    # 同一条路、报告不在，而且没有 .state.json 可读：按真实 status 记失败（判不出来就算失败）
    run_id = "orphan-noreport"
    write_job(_record(run_id=run_id, status=JobStatus.RUNNING, pid=2_000_000_000))

    assert reap_finished(reports_dir=reports) == [run_id]

    failed = get_job(run_id)
    assert failed.status is JobStatus.FAILED
    # 走的是 finish_job 的**非零**分支（jobs.py:475-483）：文案是"子进程退出码 1"，
    # 这条 run 没有 .log 所以不带"--- 日志末尾 ---"。
    # 旧断言的"报告未落盘"只出现在退出码 0 的降级分支里，本轮改成读 status 后走不到它
    assert failed.exit_code == 1
    assert failed.error == "子进程退出码 1"


def test_reap_finished_does_not_report_a_failed_orphan_as_completed(reports):
    """报告存在**不等于**运行成功：write_report 在 finally 里，失败的一轮也写。

    round 1 给孤儿一律填 exit_code=0，于是这条路上每个失败孤儿都被记成 COMPLETED。
    本轮改成读 runner 自己写下的 .state.json 的 status。
    """
    run_id = "orphan-failed"
    (reports / f"{run_id}.json").write_text("{}", encoding="utf-8")
    (reports / f"{run_id}.state.json").write_text(
        json.dumps({"run_id": run_id, "status": "failed", "error": "gateway 500"}),
        encoding="utf-8",
    )
    write_job(_record(run_id=run_id, status=JobStatus.RUNNING, pid=2_000_000_000))

    assert reap_finished(reports_dir=reports) == [run_id]

    done = get_job(run_id)
    assert done.status is JobStatus.FAILED
    assert done.exit_code == 1
    # 闸门照样重开：收尸和判成败是两件事，失败也不能占着名额
    assert running_jobs(reports_dir=reports) == []
