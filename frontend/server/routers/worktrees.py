"""U11 worktree management APIs.

git worktree 清单 + 操作面：创建（detached）/删除（安全链）/列表（git 单源真相+对账）。
列表对非 git 项目降级为 git=false 空列表（合法状态而非错误，避免前端控制台噪音）；
创建/删除仍对非 git 目录 400（动作无法进行）。

response_model 齐备——U12 契约单源的首个完整类型化路由（前端经 schema.d.ts 消费）。
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from agents.core.worktree import (
    WorktreeError,
    WorktreeEntry as WorktreeEntryData,
    create_worktree,
    list_worktrees,
    remove_worktree,
)

router = APIRouter(tags=["worktrees"])


class WorktreeCreateRequest(BaseModel):
    name: Optional[str] = None
    ref: Optional[str] = None


class WorktreeEntry(BaseModel):
    project: str
    directory: str
    kind: str                     # root | linked
    head: Optional[str] = None
    branch: Optional[str] = None
    name: Optional[str] = None    # 清单登记名（外部创建的 worktree 为 null）
    created_at: Optional[str] = None
    managed: bool                 # 是否由 MyCode 清单管理

    @classmethod
    def from_data(cls, e: WorktreeEntryData) -> "WorktreeEntry":
        return cls(**e.to_dict())


class WorktreeListResponse(BaseModel):
    worktrees: list[WorktreeEntry]
    git: bool                     # false = 项目不是 git 仓库（无 worktree 能力）


class WorktreeRemoveResponse(BaseModel):
    success: bool


@router.get("/api/projects/{cwd:path}/worktrees", response_model=WorktreeListResponse)
def api_list_worktrees(cwd: str) -> WorktreeListResponse:
    try:
        entries = list_worktrees(cwd)
    except WorktreeError:
        # 非 git 仓库/目录不存在 → 能力降级而非错误（UI 隐藏 worktree 入口）
        return WorktreeListResponse(worktrees=[], git=False)
    return WorktreeListResponse(worktrees=[WorktreeEntry.from_data(e) for e in entries], git=True)


@router.post("/api/projects/{cwd:path}/worktrees", response_model=WorktreeEntry)
def api_create_worktree(cwd: str, data: WorktreeCreateRequest) -> WorktreeEntry:
    try:
        entry = create_worktree(cwd, name=data.name, ref=data.ref)
    except WorktreeError as e:
        raise HTTPException(status_code=400, detail=e.message)
    return WorktreeEntry.from_data(entry)


@router.delete("/api/worktrees/{directory:path}", response_model=WorktreeRemoveResponse)
def api_remove_worktree(directory: str, force: bool = False) -> WorktreeRemoveResponse:
    try:
        remove_worktree(directory, force=force)
    except WorktreeError as e:
        if e.force_required:
            # 脏目录需显式 force——409 带标记，前端二次确认
            raise HTTPException(
                status_code=409,
                detail={"message": e.message, "force_required": True},
            )
        raise HTTPException(status_code=400, detail=e.message)
    return WorktreeRemoveResponse(success=True)
