from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from agents.core.snapshot_service import FileDiff, SnapshotService

if TYPE_CHECKING:
    pass


@dataclass
class FileChange:
    path: str
    status: str
    target_hash: str | None = None
    current_hash: str | None = None
    patch: str = ""

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "status": self.status,
            "target_hash": self.target_hash,
            "current_hash": self.current_hash,
            "patch": self.patch,
        }


@dataclass
class RevertPlan:
    id: str
    session_id: str
    snapshot_id: str
    original_snapshot_id: str
    changes: list[FileChange]
    created_at: int
    expires_at: int
    message_id: str | None = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "session_id": self.session_id,
            "snapshot_id": self.snapshot_id,
            "original_snapshot_id": self.original_snapshot_id,
            "changes": [c.to_dict() for c in self.changes],
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "message_id": self.message_id,
        }

    def is_expired(self) -> bool:
        return time.time() > self.expires_at


@dataclass
class RevertResult:
    plan_id: str
    restored_files: list[str]
    action: str

    def to_dict(self) -> dict:
        return {
            "plan_id": self.plan_id,
            "restored_files": self.restored_files,
            "action": self.action,
        }


class RevertService:
    def __init__(self, project_root: str | Path, snapshot_dir: str | Path):
        self.project_root = Path(project_root).resolve()
        self.snapshot_dir = Path(snapshot_dir)
        self._snapshot_service = SnapshotService(project_root, snapshot_dir)
        self._plans: dict[str, RevertPlan] = {}

    def _generate_plan_id(self) -> str:
        return f"plan_{int(time.time())}_{secrets.token_hex(4)}"

    async def stage(
        self,
        session_id: str,
        snapshot_id: str,
        message_id: str | None = None,
    ) -> RevertPlan:
        original_snapshot = await self._snapshot_service.capture(
            session_id,
            label="Before revert",
        )
        target_manifest = await self._snapshot_service.get_manifest(snapshot_id)
        inspection = await self._snapshot_service.inspect(snapshot_id)
        changes = []
        for file_diff in inspection.files:
            changes.append(
                FileChange(
                    path=file_diff.path,
                    status=file_diff.status,
                    patch=file_diff.patch,
                )
            )
        plan = RevertPlan(
            id=self._generate_plan_id(),
            session_id=session_id,
            snapshot_id=snapshot_id,
            original_snapshot_id=original_snapshot.id,
            changes=changes,
            created_at=int(time.time()),
            expires_at=int(time.time()) + 15 * 60,
            message_id=message_id,
        )
        self._plans[plan.id] = plan
        return plan

    async def commit(self, plan_id: str) -> RevertResult:
        plan = self._plans.get(plan_id)
        if not plan:
            raise ValueError(f"Plan {plan_id} not found or expired")
        if plan.is_expired():
            del self._plans[plan_id]
            raise ValueError("Plan expired, please stage again")
        restored_files = []
        files_to_restore = []
        files_to_delete = []
        for change in plan.changes:
            if change.status in ("added", "modified"):
                files_to_restore.append(change.path)
            elif change.status == "deleted":
                files_to_delete.append(change.path)
        if files_to_restore:
            await self._snapshot_service.restore(plan.snapshot_id, files_to_restore)
            restored_files.extend(files_to_restore)
        if files_to_delete:
            for file_path in files_to_delete:
                full_path = self.project_root / file_path
                if full_path.exists():
                    if full_path.is_dir():
                        import shutil
                        shutil.rmtree(full_path)
                    else:
                        full_path.unlink()
            restored_files.extend(files_to_delete)
        del self._plans[plan_id]
        return RevertResult(
            plan_id=plan_id,
            restored_files=restored_files,
            action="committed",
        )

    async def clear(self, plan_id: str) -> RevertResult:
        plan = self._plans.get(plan_id)
        if not plan:
            raise ValueError(f"Plan {plan_id} not found or expired")
        restored_files = []
        files_to_restore = []
        for change in plan.changes:
            if change.status in ("added", "modified"):
                files_to_restore.append(change.path)
        if files_to_restore:
            await self._snapshot_service.restore(plan.original_snapshot_id, files_to_restore)
            restored_files.extend(files_to_restore)
        del self._plans[plan_id]
        return RevertResult(
            plan_id=plan_id,
            restored_files=restored_files,
            action="cleared",
        )

    def get_plan(self, plan_id: str) -> RevertPlan | None:
        return self._plans.get(plan_id)

    def list_plans(self, session_id: str | None = None) -> list[RevertPlan]:
        plans = list(self._plans.values())
        if session_id:
            plans = [p for p in plans if p.session_id == session_id]
        return [p for p in plans if not p.is_expired()]

    def cleanup_expired(self) -> int:
        expired = [pid for pid, p in self._plans.items() if p.is_expired()]
        for pid in expired:
            del self._plans[pid]
        return len(expired)
