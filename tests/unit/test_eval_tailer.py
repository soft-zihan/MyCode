"""tailer：子进程写文件，后端读文件转 WS。

它出问题的方式很阴——不报错，只是页面永远停在半路。所以这里钉的不是"能转发"，
而是三条会静默失效的性质：增量（不重发）、收尾（进程退出前最后几条不能丢）、
容错（一个订阅者炸了不能带走整轮）。
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from eval.common.events import JsonlEventSink
from eval.common.jobs import JobRecord, JobStatus, get_job, write_job
from eval.common.tailer import JobTailer

_server_dir = str(Path(__file__).parent.parent.parent / "frontend" / "server")
if _server_dir not in sys.path:
    sys.path.insert(0, _server_dir)


@pytest.fixture
def reports(tmp_path, monkeypatch):
    monkeypatch.setattr("eval.common.jobs.REPORTS_DIR", tmp_path)
    return tmp_path


def _job(run_id: str, status: JobStatus = JobStatus.RUNNING) -> None:
    write_job(
        JobRecord(
            run_id=run_id,
            benchmark="gaia",
            argv=[".venv/bin/python", "-m", "eval.gaia.runner"],
            params={},
            git_revision="abc1234",
            status=status,
            pid=os.getpid(),
            started_at="2026-10-18T00:00:00+08:00",
            finished_at=None,
            exit_code=None,
            report_path=None,
            error=None,
        )
    )


def _sink(reports, run_id: str) -> JsonlEventSink:
    return JsonlEventSink(reports / f"{run_id}.events.jsonl", run_id=run_id)


def test_poll_once_broadcasts_new_events(reports):
    _job("r1")
    _sink(reports, "r1").emit("eval/task_started", task_id="t1")
    got: list[dict] = []

    n = JobTailer(got.append).poll_once()

    assert n == 1
    assert got[0]["type"] == "eval/task_started"
    assert got[0]["task_id"] == "t1"
    assert got[0]["run_id"] == "r1"
    # WS 订阅者按 session_id 过滤；评测不属于任何会话，必须显式是 None
    # （broadcast_event 里 is_global_event 靠 eval/ 前缀放行，但字段仍要补齐）
    assert got[0]["session_id"] is None


def test_poll_once_is_incremental(reports):
    _job("r1")
    sink = _sink(reports, "r1")
    sink.emit("eval/task_started", task_id="t1")
    got: list[dict] = []
    tailer = JobTailer(got.append)

    tailer.poll_once()
    tailer.poll_once()  # 没有新事件就不能重发，否则前端会看到重复行
    sink.emit("eval/task_started", task_id="t2")
    tailer.poll_once()

    assert [e["task_id"] for e in got] == ["t1", "t2"]


def test_poll_once_picks_up_a_job_started_later(reports):
    got: list[dict] = []
    tailer = JobTailer(got.append)
    assert tailer.poll_once() == 0

    _job("late")
    _sink(reports, "late").emit("eval/run_started", benchmark="harbor")

    assert tailer.poll_once() == 1
    assert got[0]["run_id"] == "late"


def test_poll_once_ignores_jobs_that_are_not_active(reports):
    # 已完成的 run 不再跟踪：否则每次重启都要把历史事件重播一遍
    _job("done", JobStatus.COMPLETED)
    _sink(reports, "done").emit("eval/run_started")
    got: list[dict] = []

    assert JobTailer(got.append).poll_once() == 0
    assert got == []


def test_poll_once_drains_then_drops_a_finished_job(reports):
    _job("r1")
    sink = _sink(reports, "r1")
    sink.emit("eval/task_started", task_id="t1")
    got: list[dict] = []
    tailer = JobTailer(got.append)
    tailer.poll_once()

    # 子进程写完最后一条就退出，job 记录翻成 completed——最后那几条不能丢
    sink.emit("eval/run_finished", status="completed")
    sink.close()
    _job("r1", JobStatus.COMPLETED)

    assert tailer.poll_once() == 1
    assert got[-1]["type"] == "eval/run_finished"
    # 这一轮只是把剩余事件排干，还没摘：摘掉跟踪发生在"不在活跃表且没有新事件"
    # 的**下一轮**——正是这个延后保证了子进程退出前的最后几条事件不会丢
    assert tailer.tracked() == ["r1"]
    # 下一轮：不活跃且无新事件 → 摘掉，offset 表不能无限长
    assert tailer.poll_once() == 0
    assert tailer.tracked() == []


def test_poll_once_tolerates_a_missing_events_file(reports):
    # Popen 刚返回、子进程还没建文件的那一小段窗口
    _job("r1")

    assert JobTailer(lambda e: None).poll_once() == 0


def test_poll_once_reaps_a_child_that_exited_on_its_own(reports):
    """tailer 是生产路径上唯一**周期性**收尸的地方——start_job 只在"用户点启动"时收一次。

    少了这一步：跑完的评测状态永远停在 running、僵尸永远被 pid_alive 判成活的，
    MAX_CONCURRENT_JOBS=1 的闸门永久关着，用户必须重启后端才能跑第二轮。
    """
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    run_id = "r1"
    # 报告得真存在：否则退出码 0 会被 finish_job 降级成 FAILED（jobs.py 的
    # "子进程退出码 0，但报告未落盘"分支，另有 test_eval_jobs.py 专测它）。
    # 这条要钉的是"tailer 周期性收尸"，不是那个降级——同 test_eval_jobs.py:560
    (reports / f"{run_id}.json").write_text("{}", encoding="utf-8")
    write_job(
        JobRecord(
            run_id=run_id,
            benchmark="gaia",
            argv=[],
            params={},
            git_revision="abc1234",
            status=JobStatus.RUNNING,
            pid=proc.pid,  # 必须是**真**子进程且不能先 proc.wait()：
            started_at="2026-10-18T00:00:00+08:00",  # 先 wait 就收过了，waitpid 会 ChildProcessError
        )
    )
    tailer = JobTailer(lambda e: None)

    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and get_job(run_id).status is JobStatus.RUNNING:
            tailer.poll_once()
            time.sleep(0.05)

        done = get_job(run_id)
        assert done.status is JobStatus.COMPLETED
        assert done.exit_code == 0
        # 收完就不再活跃，跟踪表要放手，offset 表不能无限长
        assert tailer.poll_once() == 0
        assert tailer.tracked() == []
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()  # 已被 reap_finished 收过时，CPython 的 _try_wait 会吞掉 ChildProcessError


def test_a_broadcast_error_does_not_kill_the_rest_of_the_batch(reports):
    _job("r1")
    sink = _sink(reports, "r1")
    sink.emit("eval/task_started", task_id="t1")
    sink.emit("eval/task_started", task_id="t2")
    seen: list[dict] = []

    def flaky(event: dict) -> None:
        seen.append(event)
        raise RuntimeError("subscriber gone")

    n = JobTailer(flaky).poll_once()

    assert n == 2 and [e["task_id"] for e in seen] == ["t1", "t2"]


async def test_run_polls_until_cancelled(reports):
    _job("r1")
    got: list[dict] = []
    tailer = JobTailer(got.append, poll_interval_s=0.01)
    task = asyncio.create_task(tailer.run())

    _sink(reports, "r1").emit("eval/run_started", benchmark="gaia")
    for _ in range(200):
        if got:
            break
        await asyncio.sleep(0.01)

    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert task.cancelled()
    assert got and got[0]["type"] == "eval/run_started"


def test_the_backend_exposes_one_public_broadcast_entry_point():
    # main.py 的 lifespan 里是延迟 import，名字写错只会在启动时炸一次、
    # 而且炸在 try 里被吞成一行 non-fatal 日志——页面从此不更新，没人知道为什么。
    from routers.eval import broadcast_eval_event

    assert callable(broadcast_eval_event)
