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
from eval.common.jobs import JobRecord, JobStatus, get_job, list_jobs, write_job
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


# ── Task 6 复审第 1 轮：run() 的护栏 + 一轮只扫一次索引 ─────────────────
# 两条都是"静默失效"型：跑起来看不出问题，出事时也没有任何输出。


async def _wait_until(holds, timeout=3.0, step=0.005) -> bool:
    """有界等待：条件成立返回 True，超时返回最后一次判定（由调用方 assert）。

    不能裸 `await asyncio.sleep(0.2)` 赌"这时候总该跑完两轮了"——那是在赌 CI 的调度，
    而且赌输的方式是间歇性的；也不能不 sleep 干转，run() 就在同一个循环里，
    不让出去它一轮都跑不完。
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if holds():
            return True
        await asyncio.sleep(step)
    return holds()


def _raise_once_then_behave(tailer, monkeypatch, exc: Exception | None = None) -> dict:
    """把**这个实例**的 poll_once 换成"第一次抛、之后照常"，返回计数字典。

    默认抛 FileNotFoundError，因为那不是假想的触发路径：read_events 只在 exists()
    之后裸调 stat() / open() / read()（events.py:67-74），文件在这两步之间被清掉
    （清理脚本、git clean、手工删旧 run）就是它直接穿出 poll_once。
    """
    polls = {"n": 0}
    real = tailer.poll_once

    def wrapper() -> int:
        polls["n"] += 1
        if polls["n"] == 1:
            raise exc if exc is not None else FileNotFoundError("events 文件读到一半没了")
        return real()

    monkeypatch.setattr(tailer, "poll_once", wrapper)
    return polls


async def test_run_survives_a_poll_that_raises(reports, monkeypatch):
    """一轮 poll 炸了不能带走 tailer。

    后果比"页面冻结在 half-way"更糟：main.py 持着这个 task 的唯一强引用，asyncio 那句
    "Task exception was never retrieved" 要到 GC 才可能打出来，也就是关机时——冻结期间
    运维拿到的是**零**诊断。而 tailer 是系统里唯一的周期收尸者，它一死，
    MAX_CONCURRENT_JOBS=1 的闸门要等后端重启才重开。
    """
    _job("r1")
    _sink(reports, "r1").emit("eval/run_started", benchmark="gaia")
    got: list[dict] = []
    tailer = JobTailer(got.append, poll_interval_s=0.005)
    polls = _raise_once_then_behave(tailer, monkeypatch)
    task = asyncio.create_task(tailer.run())

    try:
        # 等到第二轮**且**它真的广播了：只等一轮区分不出"活下来了"和"还没来得及死"
        assert await _wait_until(lambda: polls["n"] >= 2 and got)
        # 还活着，而且没有以取消收场
        assert not task.done() and not task.cancelled()
        # 抛掉的那一轮没把后面的事件一起带走
        assert got[0]["type"] == "eval/run_started"
        assert got[0]["run_id"] == "r1"
        assert got[0]["session_id"] is None
    finally:
        # 断言失败的路径上也得收掉，否则漏一个还在转的 task，后面的测试会变飘
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_run_is_still_cancellable_after_a_poll_failure(reports, monkeypatch):
    """护栏不能顺手把 CancelledError 一起吞掉。

    lifespan 关机时对这个 task 调 .cancel()。except Exception 抓不到 CancelledError
    （3.8 起它挂在 BaseException 下；本机 3.12.3 已核对 MRO），所以取消照常生效——
    但那是**解释器**的性质，不是这段代码的性质：谁把它改成 except BaseException
    或者加个裸 except，关机就会永久挂在这一句上。
    """
    _job("r1")
    tailer = JobTailer(lambda e: None, poll_interval_s=0.005)
    polls = _raise_once_then_behave(tailer, monkeypatch, RuntimeError("boom"))
    task = asyncio.create_task(tailer.run())

    try:
        assert await _wait_until(lambda: polls["n"] >= 2)

        task.cancel()
        # 有界等待：真被吞了的话 `await task` 就是死等到 pytest 超时，
        # 报出来的是 timeout 而不是"取消没生效"
        assert await _wait_until(lambda: task.cancelled(), timeout=2.0)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def test_poll_once_scans_the_index_once(reports, monkeypatch):
    """一轮只扫一次索引：收尸和算活跃共用同一份快照。

    list_jobs 要遍历整个 reports 目录、逐个解析 job.json，本机 7 条记录实测
    23.4 / 24.0 / 28.1 ms（3×200 次，热缓存）。扫两遍就是每 0.5s 往事件循环上压
    ~47-56ms，而同一个循环还在流式吐 token、发 WS 帧。
    少了这条断言，未来某次改动把 records= 摘掉、悄悄退回双扫，不会有任何测试变红。
    """
    _job("r1")
    calls: list = []

    def counting(*args, **kwargs):
        calls.append((args, kwargs))
        return list_jobs(*args, **kwargs)

    # 两个名字都得换：tailer 用 `from ... import list_jobs` 持着自己那份绑定，
    # 而 reap_finished / running_jobs 在 jobs.py 内部调的是另一份。只钉 tailer 这一份，
    # "保留 fetch、把 records= 摘掉"这条最可能的退回路径就测不出来——实测过：那种
    # 退回版真的扫了 3 次，只钉 tailer 绑定的版本仍然是绿的
    monkeypatch.setattr("eval.common.tailer.list_jobs", counting)
    monkeypatch.setattr("eval.common.jobs.list_jobs", counting)

    JobTailer(lambda e: None).poll_once()

    assert len(calls) == 1


def test_the_backend_exposes_one_public_broadcast_entry_point():
    # main.py 的 lifespan 里是延迟 import，名字写错只会在启动时炸一次、
    # 而且炸在 try 里被吞成一行 non-fatal 日志——页面从此不更新，没人知道为什么。
    from routers.eval import broadcast_eval_event

    assert callable(broadcast_eval_event)
