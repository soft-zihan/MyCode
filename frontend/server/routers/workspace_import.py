"""把远端仓库导入**本实例**的工作区根，并注册为 project。

这个端点在本地实例和云端实例上是同一份代码——「推到云端」的实现就是本地后端把
请求转发到云端实例的这个端点（见 routers/cloud.py）。所以这里不该出现任何
kubectl / docker：克隆发生在哪里，取决于这份代码此刻跑在哪里。

repo_url 是用户输入，校验与拒绝的理由都在 agents/core/workspace_import.py 顶部。
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from agents.core.workspace_import import (
    ImportedWorkspace,
    WorkspaceImportError,
    import_workspace,
    list_workspaces,
    workspace_root,
)

router = APIRouter(tags=["workspace-import"])


class ImportRequest(BaseModel):
    repo_url: str
    ref: Optional[str] = None
    name: Optional[str] = None


class ImportResponse(BaseModel):
    directory: str
    name: str
    repo_url: str
    ref: Optional[str] = None
    head: Optional[str] = None
    branch: Optional[str] = None
    created: bool               # true = 新克隆；false = 已存在，做了 fetch + 切分支

    @classmethod
    def from_data(cls, r: ImportedWorkspace) -> "ImportResponse":
        return cls(**r.to_dict())


class WorkspaceEntry(BaseModel):
    name: str
    directory: str
    git: bool
    head: Optional[str] = None
    branch: Optional[str] = None


class WorkspaceListResponse(BaseModel):
    root: str
    workspaces: list[WorkspaceEntry]


@router.post("/api/workspace/import", response_model=ImportResponse)
def api_import_workspace(data: ImportRequest):
    try:
        return ImportResponse.from_data(
            import_workspace(data.repo_url, ref=data.ref, name=data.name)
        )
    except WorkspaceImportError as e:
        return JSONResponse(status_code=400, content={"detail": e.message})


@router.get("/api/workspace/imports", response_model=WorkspaceListResponse)
def api_list_imported_workspaces() -> WorkspaceListResponse:
    items: list[dict[str, Any]] = list_workspaces()
    return WorkspaceListResponse(
        root=str(workspace_root()),
        workspaces=[WorkspaceEntry(**w) for w in items],
    )
