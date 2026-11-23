"""周期循环：准入 → 查 Job 状态 → tail 事件 → 终态落地。

形状照 eval/common/tailer.py：poll_once 做一轮，run 包一层「一轮失败不能带走循环」。
一条坏任务不能停掉整个 reconcile——那会让所有任务永久卡在 running。
"""

from __future__ import annotations

import asyncio
import shutil
from typing import Any, Callable

from agents.core.workspace_import import (
    WorkspaceImportError,
    derive_name,
    import_workspace,
    workspace_root,
)

from . import sandbox
from . import task_store as ts
from .git_broker import GitBrokerError, land_task
from .tailer import TaskTailer

POLL_INTERVAL_S = 2.0
MIN_FREE_BYTES = 512 * 1024 * 1024     # PVC 剩余低于 512Mi 直接拒绝提任务
_OCCUPYING = ("provisioning", "running", "landing")


def _default_broadcast(ev: dict[str, Any], target: str | None) -> None:
    # 必须是顶层 `routers.websocket`：main.py 与 agents/core/session.py 用的都是这个名字。
    # 写成 frontend.server.routers.websocket 会加载出第二个模块对象，
    # 它的 _subscribers 永远是空的，事件广播进去就没了。
    from routers.websocket import broadcast_event
    broadcast_event(ev, target_session_id=target)


def _land(task: ts.CloudTask) -> dict[str, Any]:
    return land_task(task, workspace_root() / task.workspace_name)


def _attention() -> list[ts.CloudTask]:
    """要处理的任务：非终态的，外加「已取消但 Job 还在」的（得去删）。

    先进先出：list_tasks 是倒序（前端要最新在前），准入必须反过来，
    否则连提几个任务时后来的会插队，先提的那个饿死。
    """
    tasks = ts.list_tasks(limit=1000)
    keep = [
        t for t in tasks
        if t.status not in ts.TERMINAL_STATUSES or (t.status == "cancelled" and t.job_name)
    ]
    return sorted(keep, key=lambda t: t.created_at)


class CloudReconciler:
    def __init__(
        self,
        *,
        max_concurrency: int = 1,
        broadcast: Callable[[dict[str, Any], str | None], None] | None = None,
    ) -> None:
        self._max = max_concurrency
        self._tailer = TaskTailer(broadcast or _default_broadcast)
        self._consecutive = 0

    def poll_once(self) -> None:
        self._tailer.poll_once()
        tasks = _attention()
        running = sum(1 for t in tasks if t.status in _OCCUPYING)
        for t in tasks:
            try:
                if self._step(t, running):
                    running += 1
            except Exception as e:
                # 一条坏任务不能带走整个循环
                print(f"[CLOUD] reconcile task {t.id} failed: {e!r}", flush=True)

    def _step(self, t: ts.CloudTask, running_count: int) -> bool:
        """返回 True 表示这一轮新占了一个并发位。"""
        if t.status == "queued":
            if running_count >= self._max:
                return False
            return self._provision(t)
        if t.status == "cancelled":
            self._cleanup(t)
            return False
        if t.status == "landing":
            # 上一轮落地被服务重启打断。分支可能已建好，这里不装作能续跑，
            # 但必须放掉并发位——否则唯一的位子被一条僵尸任务永久占住。
            ts.update_task(t.id, "failed", error="落地被服务重启打断，分支可能已建好，请手动检查仓库")
            return False
        phase = sandbox.job_phase(sandbox.get_job(t.job_name))
        if phase == "NotFound":
            ts.update_task(t.id, "failed", error="沙箱被外部删除")
        elif phase == "Running" and t.status == "provisioning":
            ts.update_task(t.id, "running")
        elif phase == "Succeeded":
            self._finish(t, "succeeded")
        elif phase == "DeadlineExceeded":
            self._finish(t, "timeout")
        elif phase == "Failed":
            ts.update_task(t.id, "failed", error=f"沙箱失败：{sandbox.pod_logs(t.job_name)[-500:]}")
        return False

    def _cleanup(self, t: ts.CloudTask) -> None:
        if not t.job_name:
            return
        sandbox.delete_job(t.job_name)
        # 确认消失才清 job_name：删除是异步的，早清会让 Job 变成没人管的孤儿
        if sandbox.get_job(t.job_name) is None:
            ts.update_task(t.id, None, job_name="")

    def _provision(self, t: ts.CloudTask) -> bool:
        root = workspace_root()
        root.mkdir(parents=True, exist_ok=True)
        if shutil.disk_usage(root).free < MIN_FREE_BYTES:
            ts.update_task(t.id, "failed", error="PVC 剩余空间不足 512Mi，先清理旧工作区")
            return False
        ts.update_task(t.id, "provisioning")
        try:
            name = f"{derive_name(t.repo_url)[:40]}--{t.id}"
            import_workspace(repo_url=t.repo_url, ref=t.ref, name=name)
        except WorkspaceImportError as e:
            ts.update_task(t.id, "failed", error=e.message)
            return False
        ts.update_task(t.id, None, workspace_name=name)
        settings = sandbox.sandbox_settings()
        manifest = sandbox.render_job(
            ts.load_task(t.id), image=settings["image"], namespace=settings["namespace"],
            cpu=settings["cpu"], memory=settings["memory"], deadline_s=settings["deadline_s"],
        )
        try:
            sandbox.create_job(manifest)
        except sandbox.SandboxError as e:
            ts.update_task(t.id, "failed", error=e.message)
            return False
        ts.update_task(t.id, None, job_name=sandbox.job_name(t.id))
        return True

    def _finish(self, t: ts.CloudTask, final: str) -> None:
        ts.update_task(t.id, "landing")
        try:
            result = _land(t)
        except GitBrokerError as e:
            ts.update_task(t.id, "failed", error=e.message)
            return
        ts.update_task(t.id, final, branch=result["branch"], pr_url=result["pr_url"],
                       mergeable=result["mergeable"])

    async def run(self) -> None:
        while True:
            try:
                self.poll_once()
                self._consecutive = 0
            except Exception as e:
                self._consecutive += 1
                print(f"[CLOUD] reconcile loop error ({self._consecutive}): {e!r}", flush=True)
            await asyncio.sleep(POLL_INTERVAL_S)