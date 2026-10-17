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
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

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
        try:
            job_status = JobStatus(status) if status else JobStatus.COMPLETED
        except ValueError:
            job_status = JobStatus.FAILED
        out.append(
            JobRecord(
                run_id=run_id,
                benchmark=d.get("benchmark") or "unknown",
                params={"model": d.get("model")} if d.get("model") else {},
                status=job_status,
                started_at=d.get("started_at") or d.get("created_at"),
                finished_at=d.get("finished_at"),
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
    ordered = sorted(by_id.values(), key=lambda r: r.started_at or "", reverse=True)
    return ordered[:limit]


def running_jobs(reports_dir: Path | None = None) -> list[JobRecord]:
    return [
        r
        for r in list_jobs(limit=1000, reports_dir=reports_dir)
        if r.status in (JobStatus.RUNNING, JobStatus.QUEUED)
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
        rec.error = (rec.error or "") + "服务重启后发现进程已不存在，标记为 interrupted"
        write_job(rec, root)
        fixed.append(rec.run_id)
    return fixed
