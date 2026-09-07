"""Snapshot and Revert APIs for file-level rewind."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from agents.core.snapshot_service import SnapshotService
from agents.core.revert_service import RevertService

router = APIRouter(tags=["snapshots"])


def _get_snapshot_dir() -> Path:
    return Path.home() / ".mycode" / "snapshots"


def _get_project_root() -> Path:
    return Path.cwd()


_revert_service: RevertService | None = None


def _get_revert_service() -> RevertService:
    global _revert_service
    if _revert_service is None:
        project_root = _get_project_root()
        snapshot_dir = _get_snapshot_dir()
        _revert_service = RevertService(project_root, snapshot_dir)
    return _revert_service


class CreateSnapshotRequest(BaseModel):
    session_id: str
    label: str | None = None
    message_id: str | None = None


class StageRevertRequest(BaseModel):
    session_id: str
    snapshot_id: str
    message_id: str | None = None


class CommitRevertRequest(BaseModel):
    plan_id: str


class ClearRevertRequest(BaseModel):
    plan_id: str


class RestoreRequest(BaseModel):
    snapshot_id: str
    files: list[str] | None = None


@router.post("/api/snapshots")
async def api_create_snapshot(data: CreateSnapshotRequest) -> dict[str, Any]:
    project_root = _get_project_root()
    snapshot_dir = _get_snapshot_dir()
    svc = SnapshotService(project_root, snapshot_dir)
    try:
        snapshot = await svc.capture(
            session_id=data.session_id,
            label=data.label,
            message_id=data.message_id,
        )
        return {"success": True, "snapshot": snapshot.to_dict()}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/snapshots")
async def api_list_snapshots(session_id: str | None = None) -> dict[str, Any]:
    project_root = _get_project_root()
    snapshot_dir = _get_snapshot_dir()
    svc = SnapshotService(project_root, snapshot_dir)
    try:
        snapshots = await svc.list(session_id)
        return {"success": True, "snapshots": [s.to_dict() for s in snapshots]}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/snapshots/{snapshot_id}")
async def api_inspect_snapshot(snapshot_id: str) -> dict[str, Any]:
    project_root = _get_project_root()
    snapshot_dir = _get_snapshot_dir()
    svc = SnapshotService(project_root, snapshot_dir)
    try:
        inspection = await svc.inspect(snapshot_id)
        return {"success": True, "inspection": inspection.to_dict()}
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/snapshots/{snapshot_id}/restore")
async def api_restore_snapshot(snapshot_id: str, data: RestoreRequest) -> dict[str, Any]:
    project_root = _get_project_root()
    snapshot_dir = _get_snapshot_dir()
    svc = SnapshotService(project_root, snapshot_dir)
    try:
        files = await svc.restore(snapshot_id, data.files)
        return {"success": True, "restored_files": files}
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/revert/stage")
async def api_revert_stage(data: StageRevertRequest) -> dict[str, Any]:
    project_root = _get_project_root()
    snapshot_dir = _get_snapshot_dir()
    svc = _get_revert_service()
    try:
        plan = await svc.stage(
            session_id=data.session_id,
            snapshot_id=data.snapshot_id,
            message_id=data.message_id,
        )
        return {"success": True, "plan": plan.to_dict()}
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/revert/commit")
async def api_revert_commit(data: CommitRevertRequest) -> dict[str, Any]:
    svc = _get_revert_service()
    try:
        result = await svc.commit(data.plan_id)
        return {"success": True, "result": result.to_dict()}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/revert/clear")
async def api_revert_clear(data: ClearRevertRequest) -> dict[str, Any]:
    svc = _get_revert_service()
    try:
        result = await svc.clear(data.plan_id)
        return {"success": True, "result": result.to_dict()}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/revert/plans")
async def api_list_revert_plans(session_id: str | None = None) -> dict[str, Any]:
    svc = _get_revert_service()
    plans = svc.list_plans(session_id)
    return {"success": True, "plans": [p.to_dict() for p in plans]}


@router.get("/api/revert/plans/{plan_id}")
async def api_get_revert_plan(plan_id: str) -> dict[str, Any]:
    svc = _get_revert_service()
    plan = svc.get_plan(plan_id)
    if not plan:
        raise HTTPException(status_code=404, detail=f"Plan {plan_id} not found")
    return {"success": True, "plan": plan.to_dict()}
