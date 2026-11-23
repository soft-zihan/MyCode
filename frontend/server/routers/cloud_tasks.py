"""云端任务 REST：薄编排，逻辑都在 frontend/server/cloud/ 里。

token 端点只回布尔不回显：PAT 一旦进过响应体，就会进浏览器 devtools、
进前端日志、进用户的截图。写入方向同理，绝不打印。
"""

from __future__ import annotations

import shutil
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from agents.core.workspace_import import WorkspaceImportError, validate_repo_url, workspace_root
from frontend.server.cloud import git_broker as gb
from frontend.server.cloud import sandbox
from frontend.server.cloud import task_store as ts
from frontend.server.cloud.tailer import read_task_events

router = APIRouter(tags=["cloud"])


class TaskCreate(BaseModel):
    repo_url: str
    prompt: str
    ref: Optional[str] = None
    setup_command: str = ""


class TaskOut(BaseModel):
    id: str
    repo_url: str
    prompt: str
    ref: Optional[str] = None
    setup_command: str
    status: str
    job_name: str
    session_id: str
    workspace_name: str
    branch: str
    pr_url: str
    mergeable: Optional[bool] = None
    error: str
    created_at: str
    updated_at: str


class TokenIn(BaseModel):
    token: str


class TokenOut(BaseModel):
    configured: bool


def _out(t: ts.CloudTask) -> TaskOut:
    return TaskOut(**t.to_dict())


def _load_or_404(task_id: str) -> ts.CloudTask:
    t = ts.load_task(task_id)
    if t is None:
        raise HTTPException(status_code=404, detail=f"任务不存在：{task_id}")
    return t


@router.post("/api/cloud/tasks", response_model=TaskOut)
async def create_task(data: TaskCreate) -> TaskOut:
    try:
        url = validate_repo_url(data.repo_url)
    except WorkspaceImportError as e:
        raise HTTPException(status_code=400, detail=e.message) from e
    if not data.prompt.strip():
        raise HTTPException(status_code=400, detail="任务描述不能为空")
    return _out(ts.create_task(url, data.prompt, data.ref, data.setup_command))


@router.get("/api/cloud/tasks", response_model=list[TaskOut])
async def list_tasks(limit: int = 50) -> list[TaskOut]:
    return [_out(t) for t in ts.list_tasks(limit=limit)]


@router.get("/api/cloud/tasks/{task_id}", response_model=TaskOut)
async def get_task(task_id: str) -> TaskOut:
    return _out(_load_or_404(task_id))


@router.delete("/api/cloud/tasks/{task_id}", response_model=TaskOut)
async def cancel_task(task_id: str) -> TaskOut:
    t = _load_or_404(task_id)
    if t.status in ts.TERMINAL_STATUSES:
        raise HTTPException(status_code=409, detail=f"任务已结束（{t.status}），无法取消")
    return _out(ts.update_task(task_id, "cancelled"))


@router.get("/api/cloud/tasks/{task_id}/events")
async def task_events(task_id: str, limit: int = 200) -> dict:
    _load_or_404(task_id)
    return {"task_id": task_id, "events": read_task_events(task_id, limit=limit)}


@router.post("/api/cloud/prune")
async def prune() -> dict:
    """清终态任务的工作区与 Job。活跃任务的工作区绝不动。"""
    pruned = 0
    freed = 0
    root = workspace_root()
    for t in ts.list_tasks(limit=1000):
        if t.status not in ts.TERMINAL_STATUSES:
            continue
        if t.job_name:
            try:
                sandbox.delete_job(t.job_name)
            except sandbox.SandboxError:
                # 本地开发那份后端没有 SA 凭据，清工作区照样要做
                pass
            ts.update_task(t.id, None, job_name="")
        if not t.workspace_name:
            continue
        ws = root / t.workspace_name
        if not ws.is_dir():
            continue
        freed += _dir_size(ws)
        shutil.rmtree(ws, ignore_errors=True)
        pruned += 1
    return {"pruned": pruned, "freed_bytes": freed}


def _dir_size(path) -> int:
    total = 0
    for p in path.rglob("*"):
        try:
            if p.is_file():
                total += p.stat().st_size
        except OSError:
            continue
    return total


@router.put("/api/cloud/git-token", response_model=TokenOut)
async def set_token(data: TokenIn) -> TokenOut:
    gb.write_token(data.token)
    return TokenOut(configured=gb.token_configured())


@router.get("/api/cloud/git-token", response_model=TokenOut)
async def get_token() -> TokenOut:
    return TokenOut(configured=gb.token_configured())