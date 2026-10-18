"""评测任务的执行层：起独立子进程、记索引、服务重启后修正状态。

为什么是子进程而不是 asyncio 任务：评测动辄几小时（harbor 25 题、GAIA 22 题），
跑在 web 服务进程里意味着服务一重启任务就死，而且状态会永远停在 running
（旧的 abort_run 只在内存对象还在时才有效）。子进程用 start_new_session 脱离
服务的进程组，服务重启不影响它。
"""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
import time
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Callable

from eval.common.events import EVENTS_PATH_ENV, RUN_ID_ENV
from eval.common.runner_base import REPORTS_DIR
from eval.common.schema import looks_like_run_report
from eval.common.service import new_run_id

logger = logging.getLogger(__name__)

JOBS_DIR_NAME = "jobs"
MAX_CONCURRENT_JOBS = 1

# run_id → 我们起的那个 Popen。**必须持着**：句柄一旦引用计数归零，Popen.__del__ 会把它
# 挂到 subprocess._active 上，此后这个进程里任何一次 Popen 构造都会先跑 subprocess._cleanup()，
# 用 waitpid(WNOHANG) 把它收走。后端是一个 uvicorn 进程同时服务评测 API 和 agent/git 路由，
# 普通只读端点（如 worktrees）就会构造 Popen——它抢在我们前面收尸，我们的 reap 就只剩
# ChildProcessError，记录永远停在 running，MAX_CONCURRENT_JOBS=1 的闸门再也打不开。
_PROCS: dict[str, Any] = {}


class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    ABORTED = "aborted"
    # 服务重启后发现进程已经没了：区别于 failed（子进程自己报错退出）
    INTERRUPTED = "interrupted"


def jobs_dir(reports_dir: Path | None = None) -> Path:
    return (reports_dir or REPORTS_DIR) / JOBS_DIR_NAME


def job_path(run_id: str, reports_dir: Path | None = None) -> Path:
    return jobs_dir(reports_dir) / f"{run_id}.job.json"


def events_path(run_id: str, reports_dir: Path | None = None) -> Path:
    return (reports_dir or REPORTS_DIR) / f"{run_id}.events.jsonl"


@dataclass
class JobRecord:
    run_id: str
    benchmark: str
    argv: list[str] = field(default_factory=list)
    params: dict[str, Any] = field(default_factory=dict)
    git_revision: str = "unknown"
    status: JobStatus = JobStatus.QUEUED
    pid: int | None = None
    started_at: str | None = None
    finished_at: str | None = None
    exit_code: int | None = None
    report_path: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["status"] = self.status.value
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "JobRecord":
        known = {f for f in cls.__dataclass_fields__}
        kwargs = {k: v for k, v in d.items() if k in known}
        status = kwargs.get("status")
        if isinstance(status, str):
            try:
                kwargs["status"] = JobStatus(status)
            except ValueError:
                kwargs["status"] = JobStatus.FAILED
        return cls(**kwargs)


def write_job(record: JobRecord, reports_dir: Path | None = None) -> Path:
    path = job_path(record.run_id, reports_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(record.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    # 原子替换：tailer 和前端随时在读这个文件，半份 JSON 会让整条索引失效
    os.replace(tmp, path)
    return path


def get_job(run_id: str, reports_dir: Path | None = None) -> JobRecord | None:
    path = job_path(run_id, reports_dir)
    if not path.exists():
        return None
    try:
        return JobRecord.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("读取 job 记录失败 %s: %s", path, exc)
        return None


# 旧 state.json 的状态词表是 RunStatus（models.py），和 JobStatus 不完全重合：
# `pending` 得映射到 QUEUED，缺失/不认识的一律 INTERRUPTED——
# 把一份读不懂的记录报成"成功跑完"是往错的方向撒谎
_LEGACY_STATUS = {
    "pending": JobStatus.QUEUED,
    "running": JobStatus.RUNNING,
    "completed": JobStatus.COMPLETED,
    "aborted": JobStatus.ABORTED,
    "failed": JobStatus.FAILED,
}


def _iso(value: Any) -> str | None:
    """float epoch → 本地时区 ISO 字符串；已经是字符串就原样返回。

    `EvalRunState` 的时间戳是 `time.time()`（float），新 `JobRecord` 的是 ISO 字符串。
    两种类型混进同一个 `sorted()` 会 TypeError——而 `list_jobs` 是这个模块存在的
    全部理由，它一炸，运行列表和并发闸门会一起没。
    """
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return datetime.fromtimestamp(float(value)).astimezone().isoformat(timespec="seconds")
    return str(value)


def _legacy_state_records(reports_dir: Path) -> list[JobRecord]:
    """旧的 gaia/hle/smoke/loca run 只有 .state.json，转成 JobRecord 让历史不断档。"""
    out: list[JobRecord] = []
    for path in reports_dir.glob("*.state.json"):
        try:
            d = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        run_id = d.get("run_id")
        if not run_id:
            continue
        status = d.get("status")
        job_status = _LEGACY_STATUS.get(status, JobStatus.INTERRUPTED)
        if status not in _LEGACY_STATUS:
            logger.warning("旧的 eval state %s 状态 %r 不认识，按 interrupted 记", path.name, status)
        out.append(
            JobRecord(
                run_id=run_id,
                benchmark=d.get("benchmark") or "unknown",
                params={"model": d.get("model")} if d.get("model") else {},
                status=job_status,
                started_at=_iso(d.get("started_at") or d.get("created_at")),
                finished_at=_iso(d.get("finished_at")),
                report_path=d.get("report_json_path"),
                error=d.get("error"),
            )
        )
    return out


def _record_from_report(data: Mapping[str, Any], path: Path) -> JobRecord | None:
    """从一份标准信封报告倒推出运行记录。

    harbor 是从 CLI 直接跑的，既没有 job.json 也没有 state.json；只认前两种来源的话，
    运行列表里就永远不会有 harbor，而它恰恰是最贵、最需要被看见的那一个。
    """
    if not looks_like_run_report(data):
        return None
    meta = data.get("meta") or {}
    generated = data.get("generated_at")
    return JobRecord(
        run_id=str(data.get("run_id") or path.stem),
        benchmark=str(data.get("benchmark") or "unknown"),
        argv=[],
        params=dict(meta.get("variables") or {}),
        git_revision=meta.get("git_revision") or "unknown",
        # 信封里没有"这次运行成没成"这个字段（ENVELOPE_KEYS 只有六个键，没有 status），
        # 而能写出报告就说明 runner 走完了写盘那一步。不要去 meta 里猜一个 status：
        # 没有任何生产者会写它，那条分支只会让人以为报告能表达失败
        status=JobStatus.COMPLETED,
        pid=None,
        started_at=generated,
        finished_at=generated,
        exit_code=None,  # 报告里不记退出码；有报告就说明它跑完了，不猜具体码
        report_path=str(path),
        error=None,
    )


# path → (mtime_ns, size, 解析结果)。报告文件写完就不再变（run_id 带时间戳，
# 一次运行一个文件），所以按 (mtime_ns, size) 缓存是安全的。
_REPORT_RECORDS: dict[str, tuple[int, int, JobRecord | None]] = {}


def _report_record(path: Path) -> JobRecord | None:
    """这份 JSON 是不是一次运行的标准信封报告；是就倒推出记录，不是就 None。

    **必须缓存**。list_jobs 被 tailer 每 0.5s 调一次，而本机实测：现在（两来源）
    一次 24.9ms；把 eval/reports/ 下那 7 份 harbor 报告（545KB 里的大头，每份 25 行）
    也解析一遍要再加约 44ms。那就是每 0.5s 往事件循环上压 ~65ms——而同一个循环
    还在流式吐 token、发 WS 帧。更糟的是它随运行次数线性增长：跑上 30 轮 harbor
    就是 ~150ms/tick。缓存之后稳态只剩每个文件一次 stat()（几十微秒），
    解析只在新文件出现时发生一次。
    """
    try:
        st = path.stat()
    except OSError:
        return None
    key = str(path)
    cached = _REPORT_RECORDS.get(key)
    if cached is not None and cached[0] == st.st_mtime_ns and cached[1] == st.st_size:
        return cached[2]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        # 不 catch UnicodeDecodeError：本任务同时把三处写盘改成了原子写，截断从此不会发生；
        # 真发生了就该炸出来，静默跳过等于把一次运行从列表里藏掉
        data = None
    record = _record_from_report(data, path) if data is not None else None
    _REPORT_RECORDS[key] = (st.st_mtime_ns, st.st_size, record)
    return record


def list_jobs(limit: int = 100, reports_dir: Path | None = None) -> list[JobRecord]:
    root = reports_dir or REPORTS_DIR
    if not root.exists():
        return []
    by_id: dict[str, JobRecord] = {}
    # 先放旧的，再用新的覆盖：同一个 run_id 以 job.json 为准
    for rec in _legacy_state_records(root):
        by_id[rec.run_id] = rec
    for path in jobs_dir(root).glob("*.job.json") if jobs_dir(root).exists() else []:
        rec = get_job(path.name[: -len(".job.json")], root)
        if rec:
            by_id[rec.run_id] = rec
    # 第三个来源：标准信封报告。harbor 从 CLI 直跑，两个索引文件都没有。
    # 优先级靠"不覆盖"实现，不是靠顺序：by_id 是 dict，前两个来源已经填过了，
    # 这里只在 run_id 还没出现时才补——所以 job.json > state.json > 报告。
    for path in sorted(root.glob("*.json")):
        if path.name.endswith(".state.json") or path.stem in by_id:
            continue
        record = _report_record(path)
        if record is not None:
            by_id[record.run_id] = record
    # 缓存只保留还存在的文件：服务是长命的，报告目录只增不减，
    # 不清的话被删掉那些报告的条目会一直挂在字典里
    for key in list(_REPORT_RECORDS):
        if not Path(key).exists():
            del _REPORT_RECORDS[key]
    # `str(...)`：_iso 归一化了旧 .state.json 那条路，但手改过或外部写进来的
    # job.json 仍可能带数值型 started_at。float 和 str 混进同一个 sorted() 会
    # TypeError，而 list_jobs 一炸，运行列表和并发闸门会一起没
    ordered = sorted(by_id.values(), key=lambda r: str(r.started_at or ""), reverse=True)
    return ordered[:limit]


def running_jobs(
    reports_dir: Path | None = None, records: list[JobRecord] | None = None
) -> list[JobRecord]:
    # records=：调用方已经扫过索引时直接复用那份快照。list_jobs 要遍历整个 reports
    # 目录并逐个解析 job.json（本机 7 条记录实测 ~24ms），tailer 一轮里既要收尸又要
    # 算活跃，扫两次就是把 ~48ms 压在事件循环上——而同一个循环还在流式吐 token、发 WS 帧。
    # 不传就跟原来一样自己扫，所有既有调用方行为不变
    if records is None:
        records = list_jobs(limit=1000, reports_dir=reports_dir)
    return [
        r
        for r in records
        # `and r.pid`：只算**我们起的、查得到死活也杀得掉**的那些。
        # 旧 .state.json 记录没有 pid，把它算进名额会让 MAX_CONCURRENT_JOBS=1 的闸门
        # 被一份陈年 state 文件永久焊死——而那份文件来自进程内 asyncio 的旧执行路径，
        # 既不由这层调度，也不能被 abort_job 中止
        if r.status in (JobStatus.RUNNING, JobStatus.QUEUED) and r.pid
    ]


def pid_alive(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # 进程存在但不属于我们（pid 复用）：当成活的，避免误标 interrupted
        return True
    except OSError:
        return False
    return True


def attach_on_startup(reports_dir: Path | None = None) -> list[str]:
    """服务启动时修正"状态说在跑、进程其实没了"的记录。

    没有这一步，被 kill -9 的任务会永远显示 running——这正是现在的行为。
    """
    root = reports_dir or REPORTS_DIR
    fixed: list[str] = []
    d = jobs_dir(root)
    if not d.exists():
        return fixed
    for path in d.glob("*.job.json"):
        rec = get_job(path.name[: -len(".job.json")], root)
        if not rec or rec.status not in (JobStatus.RUNNING, JobStatus.QUEUED):
            continue
        if pid_alive(rec.pid):
            continue
        rec.status = JobStatus.INTERRUPTED
        rec.error = "\n".join(
            part for part in (rec.error, "服务重启后发现进程已不存在，标记为 interrupted") if part
        )
        # 不收口的话这一条永远没有结束时间，页面上的耗时只能拿 now 去减，越看越长
        rec.finished_at = rec.finished_at or time.strftime("%Y-%m-%dT%H:%M:%S%z")
        write_job(rec, root)
        fixed.append(rec.run_id)
    return fixed


def _poll_exit(run_id: str, pid: int) -> int | None:
    """子进程退出了就返回退出码（被信号杀是负数），还在跑或判不了就返回 None。"""
    proc = _PROCS.get(run_id)
    if proc is not None:
        # 句柄在手上就走 poll()：它自己 waitpid，退出码直接是 int，
        # 被信号杀已经是负数——正好是 finish_job 的文案约定要的形状
        code = proc.poll()
        if code is not None:
            _PROCS.pop(run_id, None)
        return code
    # 句柄不在这个进程里（服务重启过）：只能自己 waitpid。
    # 不是我们的子进程会 ChildProcessError，那就判不了死活，交给 attach_on_startup
    try:
        reaped_pid, status = os.waitpid(pid, os.WNOHANG)
    except (ChildProcessError, OSError):
        return None
    if reaped_pid == 0:
        return None
    return os.waitstatus_to_exitcode(status)


def _orphan_exit_code(run_id: str, reports_dir: Path | None) -> int:
    """孤儿的退出码：读 runner 自己写下的 .state.json，别一律填 0。

    为什么不能填 0：EvalService.write_report 在 finally 里调，所以异常失败的一轮
    （state.status = "failed"）照样会写出 <run_id>.json；而 finish_job 只看
    "退出码 0 + 报告在不在"，于是每个失败孤儿都会被记成 COMPLETED。
    运行记录是要当回归基线的，假 COMPLETED 是静默污染。
    不用 service.load_state()：它读的是 service.py 自己的 REPORTS_DIR 模块全局，
    会无视这里收到的 reports_dir——测试里两个常量是分别 monkeypatch 的，会读错目录。
    """
    root = Path(reports_dir) if reports_dir else REPORTS_DIR
    try:
        d = json.loads((root / f"{run_id}.state.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 1
    return 0 if d.get("status") == "completed" else 1


def reap_finished(
    reports_dir: Path | None = None, records: list[JobRecord] | None = None
) -> list[str]:
    """非阻塞地收一下还挂在 RUNNING/QUEUED/ABORTED 上的子进程：死了就 finish_job。

    为什么必须自己收、不能只看 pid_alive：子进程退出后、父进程 wait() 之前是**僵尸**，
    而 os.kill(pid, 0) 对僵尸是成功的——pid_alive 会一直说"还活着"。状态于是永远停在
    running，MAX_CONCURRENT_JOBS=1 的闸门永久关着，第二次评测再也起不来。
    收一次同时解决三件事：判死、回收僵尸、拿到真实退出码
    （被信号杀是负数，正好对上 finish_job 的文案约定）。
    句柄在 _PROCS 里就走 proc.poll()，不在（服务重启过）才退回 waitpid——见 _poll_exit。
    """
    if records is None:
        records = list_jobs(limit=1000, reports_dir=reports_dir)
    reaped: list[str] = []
    for rec in records:
        # ABORTED 也要收：abort_job 只发 SIGTERM 就把状态写成 ABORTED，进程随后退出照样变僵尸。
        # 不收的话 (a) start_job 的 dying 闸门会一直把它当成"还在收尾"而焊死，
        # (b) finish_job 里那条专门给 ABORTED 的分支永远没人调，中止过的运行就没有退出码
        if rec.status not in (JobStatus.RUNNING, JobStatus.QUEUED, JobStatus.ABORTED) or not rec.pid:
            continue
        exit_code = _poll_exit(rec.run_id, rec.pid)
        if exit_code is None:
            # 判不了退出码不等于判不了死活。服务重启后 _PROCS 是空的，而那个 detached
            # 子进程已被 reparent 给 init、init 早把它收走了，waitpid 只会 ChildProcessError
            # ——退出码再也拿不到。但"进程没了"还能判：不判的话这条记录永远停在 running，
            # 闸门要等到下一次重启才由 attach_on_startup 打开，而那时它会被标成
            # interrupted，哪怕它其实成功写了报告
            #
            # `rec.run_id in _PROCS` 时不查 pid_alive：句柄在手上，poll() 返回 None 就等于
            # 孩子真活着（僵尸会被 poll 自己收走并给出退出码；已被别人收走的那条 CPython
            # 的 _internal_poll 按 ECHILD 判成 sts=0，也返回退出码而不是 None），再查一次
            # 是多余的，而且会把"句柄说在跑、pid 却查不到"的不一致当成死——生产上不会
            # 出现这种不一致，测试里的假 pid（_FakeProc 的 4321）会
            if rec.run_id in _PROCS or pid_alive(rec.pid):
                continue
            # 退出码这辈子拿不到了，但 runner 自己写下过真话：<run_id>.state.json 的 status
            exit_code = _orphan_exit_code(rec.run_id, reports_dir)
        finish_job(rec.run_id, exit_code, reports_dir)
        reaped.append(rec.run_id)
    return reaped


BENCHMARK_MODULES = {
    "gaia": "eval.gaia.runner",
    "hle": "eval.hle.runner",
    "smoke": "eval.smoke.runner",
    "loca": "eval.loca.runner",
    "harbor": "eval.harbor.runner",
}


class JobStartError(RuntimeError):
    """启动被拒（未知 benchmark、已有任务在跑）。前端映射成 4xx，不是 500。"""


def git_revision() -> str:
    """这轮跑的是哪个版本的代码。没有它，两次运行无法当基线对比。"""
    from eval.harbor.runner import git_revision as _impl

    try:
        return _impl()
    except (OSError, subprocess.SubprocessError):
        # 委托的那份实现只兜住了第一个 git 子进程，`git status --porcelain` 没兜。
        # start_job 调它的时候评测子进程可能已经起来了，让它抛出去等于
        # 把一个正在跑的评测变成没有索引、中止不掉的孤儿
        return "unknown"


def build_argv(benchmark: str, params: dict[str, Any]) -> list[str]:
    module = BENCHMARK_MODULES.get(benchmark)
    if not module:
        raise JobStartError(
            f"未知 benchmark：{benchmark!r}（可选：{', '.join(sorted(BENCHMARK_MODULES))}）"
        )
    argv = [sys.executable, "-m", module]
    for key, value in params.items():
        if value is None:
            continue
        flag = f"--{key.replace('_', '-')}"
        if isinstance(value, bool):
            # store_true 型的开关只出 flag；False 就整个不出（CLI 的默认值即 False）
            if value:
                argv.append(flag)
            continue
        if isinstance(value, (list, tuple)):
            argv += [flag, ",".join(str(v) for v in value)]
            continue
        argv += [flag, str(value)]
    return argv


def start_job(
    benchmark: str,
    params: dict[str, Any] | None = None,
    reports_dir: Path | None = None,
    popen: Callable[..., Any] | None = None,
) -> JobRecord:
    params = dict(params or {})
    # 上一轮可能已经退出了、状态却还挂着 running（没人 wait 过它）。
    # 不先收掉，闸门看到的就是过期状态：要么白关着，要么把一个僵尸当成还在收尾
    reap_finished(reports_dir)
    busy = running_jobs(reports_dir)
    if len(busy) >= MAX_CONCURRENT_JOBS:
        raise JobStartError(
            f"已有 {len(busy)} 个评测在跑（上限 {MAX_CONCURRENT_JOBS}）："
            f"{', '.join(r.run_id for r in busy)}。评测很重且共享同一个网关配额，先中止或等它跑完。"
        )
    # abort_job 只发 SIGTERM 就返回，不等进程真的死：runner 下面还挂着 harbor / docker，
    # 收尾要几秒。这段时间里 running_jobs 已经不算它了，闸门会照常放行下一轮，
    # 于是新旧两轮抢同一个网关配额
    dying = [
        r
        for r in list_jobs(limit=1000, reports_dir=reports_dir)
        if r.status is JobStatus.ABORTED and r.pid and pid_alive(r.pid)
    ]
    if dying:
        raise JobStartError(
            f"上一轮评测刚被中止、进程还在收尾（{', '.join(r.run_id for r in dying)}）。"
            "runner 下面还挂着 harbor / docker，SIGTERM 不是立刻生效的。"
            "等几秒再启动，否则两轮会抢同一个网关配额。"
        )

    run_id = new_run_id(benchmark)
    argv = build_argv(benchmark, params)
    ev_path = events_path(run_id, reports_dir)
    ev_path.parent.mkdir(parents=True, exist_ok=True)

    # 两个 env 名从 events.py 拿：sink_from_env() 就是按那两个常量读的，
    # 这里再硬编码一份字符串，一旦漂移，子进程会静默地一个事件都不写
    env = {**os.environ, RUN_ID_ENV: run_id, EVENTS_PATH_ENV: str(ev_path)}
    spawn = popen or subprocess.Popen
    revision = git_revision()
    # start_new_session=True：子进程自成一个进程组，服务被杀时不会连带被杀。
    # stdout/stderr 落文件而不是 PIPE——PIPE 没人读会写满 64KB 缓冲区把子进程堵死。
    log_path = (reports_dir or REPORTS_DIR) / f"{run_id}.log"
    with open(log_path, "ab") as log_fh:
        proc = spawn(
            argv,
            env=env,
            cwd=str(Path(__file__).resolve().parents[2]),
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )

    _PROCS[run_id] = proc

    record = JobRecord(
        run_id=run_id,
        benchmark=benchmark,
        argv=argv,
        params=params,
        git_revision=revision,
        status=JobStatus.RUNNING,
        pid=getattr(proc, "pid", None),
        started_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    )
    try:
        write_job(record, reports_dir)
    except Exception:
        _PROCS.pop(run_id, None)  # 索引没写上＝reap_finished 永远不会访问这个 run_id，不摘就漏一条句柄和一只僵尸
        # 子进程已经起来了、索引却没写上：它会占着网关配额和 docker，
        # 在 UI 里隐形、不占并发名额、也中止不掉。宁可现在杀掉。
        pid = getattr(proc, "pid", None)
        if pid:
            try:
                os.killpg(pid, signal.SIGTERM)
            except OSError:
                logger.warning("启动失败后清理子进程 %s 也没成功", pid)
        raise
    return record


def finish_job(
    run_id: str, exit_code: int | None, reports_dir: Path | None = None
) -> JobRecord | None:
    rec = get_job(run_id, reports_dir)
    if not rec:
        return None
    if rec.status == JobStatus.ABORTED:
        # 主动中止过：退出码非零是预期内的，不要覆盖成 failed
        rec.exit_code = exit_code
        rec.finished_at = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        write_job(rec, reports_dir)
        return rec
    rec.exit_code = exit_code
    rec.finished_at = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    out_dir = Path(reports_dir) if reports_dir else REPORTS_DIR
    report = out_dir / f"{run_id}.json"
    if exit_code == 0:
        rec.status = JobStatus.COMPLETED
        if report.exists():
            rec.report_path = str(report)
        elif not _report_is_optional(rec):
            # 退出码 0 却什么都没留下：页面会显示"完成"而详情页是空的，
            # 这比失败更难查，所以宁可报失败
            rec.status = JobStatus.FAILED
            rec.error = "子进程退出码 0，但报告未落盘"
    else:
        rec.status = JobStatus.FAILED
        reason = f"被信号 {-exit_code} 杀掉" if (exit_code or 0) < 0 else f"退出码 {exit_code}"
        log_path = out_dir / f"{run_id}.log"
        tail = ""
        if log_path.exists():
            lines = log_path.read_text(encoding="utf-8", errors="replace").strip().splitlines()
            tail = "\n".join(lines[-15:])
        rec.error = f"子进程{reason}" + (f"\n--- 日志末尾 ---\n{tail}" if tail else "")
    write_job(rec, reports_dir)
    return rec


# 这两种跑法本来就不写报告：--dry-run 只让 harbor 校验配置，--install-only 只装 agent
_REPORT_IS_OPTIONAL = ("dry_run", "install_only")


def _report_is_optional(rec: JobRecord) -> bool:
    return any((rec.params or {}).get(key) for key in _REPORT_IS_OPTIONAL)


def abort_job(
    run_id: str,
    reports_dir: Path | None = None,
    killer: Callable[[int, int], None] | None = None,
) -> JobRecord | None:
    rec = get_job(run_id, reports_dir)
    if not rec:
        return None
    if rec.status in (JobStatus.RUNNING, JobStatus.QUEUED) and rec.pid:
        send = killer or os.killpg
        try:
            # 发给**进程组**而不是单个 pid：start_new_session=True 让子进程自成一组
            # （pgid == pid），而 runner 自己还会起 harbor / docker 子进程，
            # 只杀 runner 会留下一堆孤儿容器
            send(rec.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError) as exc:
            logger.warning("中止 %s 失败（进程可能已退出）：%s", run_id, exc)
        rec.status = JobStatus.ABORTED
        rec.finished_at = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        write_job(rec, reports_dir)
    return rec
