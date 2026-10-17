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
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Callable

from eval.common.events import EVENTS_PATH_ENV, RUN_ID_ENV
from eval.common.runner_base import REPORTS_DIR
from eval.common.service import new_run_id

logger = logging.getLogger(__name__)

JOBS_DIR_NAME = "jobs"
MAX_CONCURRENT_JOBS = 1


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
    # `str(...)`：_iso 归一化了旧 .state.json 那条路，但手改过或外部写进来的
    # job.json 仍可能带数值型 started_at。float 和 str 混进同一个 sorted() 会
    # TypeError，而 list_jobs 一炸，运行列表和并发闸门会一起没
    ordered = sorted(by_id.values(), key=lambda r: str(r.started_at or ""), reverse=True)
    return ordered[:limit]


def running_jobs(reports_dir: Path | None = None) -> list[JobRecord]:
    return [
        r
        for r in list_jobs(limit=1000, reports_dir=reports_dir)
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

    return _impl()


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
    busy = running_jobs(reports_dir)
    if len(busy) >= MAX_CONCURRENT_JOBS:
        raise JobStartError(
            f"已有 {len(busy)} 个评测在跑（上限 {MAX_CONCURRENT_JOBS}）："
            f"{', '.join(r.run_id for r in busy)}。评测很重且共享同一个网关配额，先中止或等它跑完。"
        )

    run_id = new_run_id(benchmark)
    argv = build_argv(benchmark, params)
    ev_path = events_path(run_id, reports_dir)
    ev_path.parent.mkdir(parents=True, exist_ok=True)

    # 两个 env 名从 events.py 拿：sink_from_env() 就是按那两个常量读的，
    # 这里再硬编码一份字符串，一旦漂移，子进程会静默地一个事件都不写
    env = {**os.environ, RUN_ID_ENV: run_id, EVENTS_PATH_ENV: str(ev_path)}
    spawn = popen or subprocess.Popen
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

    record = JobRecord(
        run_id=run_id,
        benchmark=benchmark,
        argv=argv,
        params=params,
        git_revision=git_revision(),
        status=JobStatus.RUNNING,
        pid=getattr(proc, "pid", None),
        started_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    )
    write_job(record, reports_dir)
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
