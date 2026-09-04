"""Project management APIs."""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from agents.core.project import (
    list_projects,
    get_project,
    register_project,
    update_project,
    delete_project,
)

router = APIRouter(tags=["projects"])


class ProjectCreateRequest(BaseModel):
    cwd: str
    name: Optional[str] = None


class ProjectUpdateRequest(BaseModel):
    name: Optional[str] = None


@router.get("/api/projects")
def api_list_projects() -> list[dict[str, Any]]:
    """列出所有 project。"""
    projects = list_projects()
    return [p.to_dict() for p in projects]


@router.get("/api/projects/{cwd:path}")
def api_get_project(cwd: str) -> dict[str, Any]:
    """获取指定 project。"""
    project = get_project(cwd)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return project.to_dict()


@router.post("/api/projects")
def api_create_project(data: ProjectCreateRequest) -> dict[str, Any]:
    """注册 project。"""
    project = register_project(data.cwd, data.name)
    return project.to_dict()


@router.put("/api/projects/{cwd:path}")
def api_update_project(cwd: str, data: ProjectUpdateRequest) -> dict[str, Any]:
    """更新 project 信息。"""
    project = update_project(cwd, data.name)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return project.to_dict()


@router.delete("/api/projects/{cwd:path}")
def api_delete_project(cwd: str) -> dict[str, bool]:
    """删除 project（不删除其下的 session）。"""
    success = delete_project(cwd)
    if not success:
        raise HTTPException(status_code=404, detail="Project not found")
    return {"success": True}
