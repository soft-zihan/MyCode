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
