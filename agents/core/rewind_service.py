"""统一回退编排：对话 + 文件原子回退（stage/commit/clear 三阶段）。

设计原则：
- Event 是唯一数据源。每条用户消息发送前已捕获 workspace 的 git 快照，
  snapshot_id 锚定在 user_message 事件上（见 agent_loop._prepare_turn）。
- 回退锚点 = 第一条将被删除的 user_message 事件：对话截断到它之前、
  文件恢复到它发送时的快照——两者天然是同一时刻，原子一致。
- stage：生成预览（文件 diff + 对话截断信息），捕获 original 快照用于失败补偿
- commit：恢复文件 → 截断事件日志 → 截断内存 Session → 追加 rewind 标记事件
- clear：丢弃计划（stage 未动文件，无需恢复）
- commit 任一步失败 → 用 original 快照回滚已恢复的文件，保证不会
  出现"文件回退了但对话没回退"的中间态
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agents.core.session import get_session_backend
from agents.core.snapshot_service import SnapshotService

SNAPSHOT_DIR = Path.home() / ".mycode" / "snapshots"
PLAN_TTL_S = 15 * 60


@dataclass
class RewindPlan:
    id: str
    session_id: str
    workspace: str | None
    truncate_at_seq: int  # 保留 seq < truncate_at_seq 的事件
    target_message: dict[str, Any]  # 第一条将被删除的用户消息 {seq, content}
    removed_user_messages: int
    removed_events: int
    snapshot_id: str | None  # None = 目标消息无快照（仅回退对话）
    original_snapshot_id: str | None
    file_changes: list[dict] = field(default_factory=list)
    created_at: int = field(default_factory=lambda: int(time.time()))
    expires_at: int = 0

    def __post_init__(self) -> None:
        if not self.expires_at:
            self.expires_at = self.created_at + PLAN_TTL_S

    def is_expired(self) -> bool:
        return time.time() > self.expires_at

    def to_dict(self) -> dict:
        return {
            "plan_id": self.id,
            "session_id": self.session_id,
            "truncate_at_seq": self.truncate_at_seq,
            "target_message": self.target_message,
            "removed_user_messages": self.removed_user_messages,
            "removed_events": self.removed_events,
            "has_snapshot": self.snapshot_id is not None,
            "file_changes": self.file_changes,
            "expires_at": self.expires_at,
        }


class RewindService:
    """对话+文件原子回退。plans 保存在内存（stage→commit 间隔短，15 分钟过期）。"""

    def __init__(self, snapshot_dir: str | Path = SNAPSHOT_DIR):
        self.snapshot_dir = Path(snapshot_dir)
        self._plans: dict[str, RewindPlan] = {}

    def _generate_plan_id(self) -> str:
        return f"rewind_{int(time.time())}_{secrets.token_hex(4)}"

    def _locate_target(
        self,
        events: list[dict],
        turns: int | None,
        keep_user_messages: int | None,
    ) -> tuple[dict, int, int]:
        """定位第一条将被删除的 user_message。

        Returns: (target_event, keep_user_messages, removed_events)
        """
        user_msgs = [e for e in events if e.get("type") == "user_message"]
        if not user_msgs:
            raise ValueError("Session has no user messages to rewind")
        if turns is not None:
            if turns < 1:
                raise ValueError("turns must be >= 1")
            keep = len(user_msgs) - turns
        elif keep_user_messages is not None:
            keep = keep_user_messages
        else:
            raise ValueError("Either turns or keep_user_messages is required")
        keep = max(0, keep)
        if keep >= len(user_msgs):
            raise ValueError(
                f"Nothing to rewind: keeping {keep} of {len(user_msgs)} user messages"
            )
        target = user_msgs[keep]
        truncate_at_seq = target.get("seq", 0)
        removed_events = sum(1 for e in events if e.get("seq", 0) >= truncate_at_seq)
        return target, keep, removed_events

    def _workspace_from_events(self, events: list[dict]) -> str | None:
        for event in events:
            if event.get("type") == "session/created" and event.get("cwd"):
                return event.get("cwd")
        return None

    async def stage(
        self,
        session_id: str,
        *,
        turns: int | None = None,
        keep_user_messages: int | None = None,
        session: Any = None,
        workspace: str | None = None,
    ) -> RewindPlan:
        """生成回退计划（预览，不修改任何状态）。"""
        self.cleanup_expired()
        events = list(session._log) if session is not None else get_session_backend().load_all_events(session_id)
        if not events:
            raise ValueError(f"Session {session_id} not found or empty")

        target, keep, removed_events = self._locate_target(events, turns, keep_user_messages)
        truncate_at_seq = target.get("seq", 0)

        if workspace is None and session is not None:
            workspace = session.projections.get("cwd")
        if workspace is None:
            workspace = self._workspace_from_events(events)

        snapshot_id = target.get("snapshot_id")
        file_changes: list[dict] = []
        original_snapshot_id = None
        if snapshot_id and workspace:
            svc = SnapshotService(workspace, self.snapshot_dir)
            try:
                original = await svc.capture(session_id, label="Before rewind")
                original_snapshot_id = original.id
                inspection = await svc.inspect(snapshot_id)
                file_changes = [f.to_dict() for f in inspection.files]
            except FileNotFoundError:
                # 快照 manifest 丢失：降级为仅回退对话，明确告知
                snapshot_id = None
                original_snapshot_id = None

        plan = RewindPlan(
            id=self._generate_plan_id(),
            session_id=session_id,
            workspace=workspace,
            truncate_at_seq=truncate_at_seq,
            target_message={"seq": truncate_at_seq, "content": target.get("content", "")},
            removed_user_messages=len([e for e in events if e.get("type") == "user_message"]) - keep,
            removed_events=removed_events,
            snapshot_id=snapshot_id,
            original_snapshot_id=original_snapshot_id,
            file_changes=file_changes,
        )
        self._plans[plan.id] = plan
        return plan

    async def commit(self, plan_id: str, *, session: Any = None) -> dict:
        """执行回退：恢复文件 → 截断事件日志 → 追加 rewind 标记事件。"""
        plan = self._plans.pop(plan_id, None)
        if not plan:
            raise ValueError(f"Rewind plan {plan_id} not found or expired")
        if plan.is_expired():
            raise ValueError("Rewind plan expired, please stage again")

        restored_files: list[str] = []
        backend = get_session_backend()
        try:
            # 1. 恢复文件到目标快照
            if plan.snapshot_id and plan.workspace and plan.file_changes:
                svc = SnapshotService(plan.workspace, self.snapshot_dir)
                paths = [c["path"] for c in plan.file_changes]
                restored_files = await svc.restore(plan.snapshot_id, paths)

            # 2. 截断持久化事件日志
            backend.truncate(plan.session_id, plan.truncate_at_seq)

            # 3. 截断内存 Session 并追加 rewind 标记事件
            marker = {
                "target_seq": plan.truncate_at_seq,
                "snapshot_id": plan.snapshot_id,
                "restored_files": len(restored_files),
                "removed_events": plan.removed_events,
                "removed_user_messages": plan.removed_user_messages,
            }
            if session is not None:
                session.truncate_events_to(plan.truncate_at_seq)
                session.append("rewind", marker)
            else:
                backend.append(plan.session_id, {
                    "type": "rewind",
                    "seq": plan.truncate_at_seq,
                    "time": int(time.time() * 1000),
                    "session_id": plan.session_id,
                    **marker,
                })
        except Exception:
            # 补偿：文件已恢复但对话截断失败 → 回滚文件到 stage 时状态
            if restored_files and plan.original_snapshot_id and plan.workspace:
                svc = SnapshotService(plan.workspace, self.snapshot_dir)
                await svc.restore(plan.original_snapshot_id, restored_files)
            raise

        return {
            "plan_id": plan.id,
            "truncate_at_seq": plan.truncate_at_seq,
            "removed_events": plan.removed_events,
            "removed_user_messages": plan.removed_user_messages,
            "restored_files": restored_files,
        }

    async def clear(self, plan_id: str) -> dict:
        """取消回退计划（stage 未修改文件，直接丢弃）。"""
        plan = self._plans.pop(plan_id, None)
        if not plan:
            raise ValueError(f"Rewind plan {plan_id} not found or expired")
        return {"plan_id": plan_id, "status": "cleared"}

    def cleanup_expired(self) -> int:
        expired = [pid for pid, p in self._plans.items() if p.is_expired()]
        for pid in expired:
            del self._plans[pid]
        return len(expired)


_service: RewindService | None = None


def get_rewind_service() -> RewindService:
    global _service
    if _service is None:
        _service = RewindService()
    return _service
