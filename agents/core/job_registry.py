"""子代理 Job 注册表（U3a，对齐 v2 job.ts:128-140,253-299 极简持久化哲学）。

进程内注册表：ID=sub_session_id，done/backgrounded 双 asyncio.Event，join 去重。
**不建表**——恢复面靠事件流（sub_agent/start、sub_agent/end、subagent/completed 均落盘），
注册表只服务进程内的 block/join/转后台交互。U4 硬中止在此扩展 cancel 语义。
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from agents.agent import Agent


@dataclass
class SubAgentJob:
    """一次子代理 run 的进程内句柄。

    - task: 全生命周期 runner task（run→封装→sub_agent/end→完成通知）
    - done: run 结束（任何终态）时 set；result/state 随即可读
    - backgrounded: 后台化标记——发起即后台（background=true）或前台转后台；
      决定完成后是否向父会话注入 subagent/completed synthetic 事件
    - notification_id: 预分配幂等 ID（done 回调与 cancel 竞态可能双触发，v2 job.ts:38,199-209）
    """

    sub_session_id: str
    parent_session_id: str
    agent_type: str
    description: str
    sub_agent: "Agent"
    task: asyncio.Task | None = None
    done: asyncio.Event = field(default_factory=asyncio.Event)
    backgrounded: asyncio.Event = field(default_factory=asyncio.Event)
    state: dict[str, Any] = field(default_factory=dict)
    result: Any = None
    notification_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    cancel_requested: bool = False

    def mark_background(self) -> None:
        self.backgrounded.set()

    def request_cancel(self) -> None:
        """U4 硬中止：软 abort（子 Agent 置位标志+取消其当前 task，run_once 在
        step 边界优雅收尾并补 turn/end{reason:"aborted"}）+ 硬 cancel 生命周期
        task 兜底（模型流等待中的 CancelledError 经 _await_sub_agent_run 级联杀
        run_task）。v2 只有软中止（job.ts:413-441 fiber interrupt），我们双通道。
        """
        self.cancel_requested = True
        if self.sub_agent is not None:
            self.sub_agent.abort()
        if self.task is not None and not self.task.done():
            self.task.cancel()


_jobs: dict[str, SubAgentJob] = {}


def register_job(job: SubAgentJob) -> SubAgentJob:
    _jobs[job.sub_session_id] = job
    return job


def get_job(sub_session_id: str) -> SubAgentJob | None:
    return _jobs.get(sub_session_id)


def pop_job(sub_session_id: str) -> SubAgentJob | None:
    return _jobs.pop(sub_session_id, None)


def jobs_for_parent(parent_session_id: str) -> list[SubAgentJob]:
    return [j for j in _jobs.values() if j.parent_session_id == parent_session_id]


def clear_jobs() -> None:
    """测试隔离用。"""
    _jobs.clear()
