"""Git 分支清单与切换 API。

response_model 齐备——与 worktrees.py 同一契约纪律（前端经 schema.d.ts 消费）。
字段用 snake_case，跟同属 `/api/projects/{cwd}/...` 家族的 worktrees 保持一致。

脏工作树的失败不用 HTTPException：它只能带一个 detail 字符串，而前端需要区分
「工作树脏，先去提交」和「没有这个分支」这两种完全不同的处置，所以走 JSONResponse，
让 detail 与 dirty 在 body 里并列。
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from agents.core.branches import (
    BranchError,
    BranchInfo,
    BranchRef,
    checkout,
    list_branches,
)

router = APIRouter(tags=["branches"])


class BranchEntry(BaseModel):
    name: str
    kind: str                   # local | remote
    head: str | None = None
    current: bool = False

    @classmethod
    def from_data(cls, b: BranchRef) -> "BranchEntry":
        return cls(**b.to_dict())


class BranchListResponse(BaseModel):
    project: str
    git: bool                   # false = 不是 git 仓库（前端隐藏分支入口）
    current: str | None = None
    detached: bool = False
    dirty: bool = False
    remote_url: str | None = None
    branches: list[BranchEntry]
    fetch_error: str | None = None

    @classmethod
    def from_info(cls, info: BranchInfo) -> "BranchListResponse":
        data = info.to_dict()
        data["branches"] = [BranchEntry.from_data(b) for b in info.branches]
        return cls(**data)


class CheckoutRequest(BaseModel):
    ref: str


def _error_response(e: BranchError) -> JSONResponse:
    return JSONResponse(
        status_code=400,
        content={"detail": e.message, "dirty": e.dirty, "operation": e.operation},
    )


@router.get("/api/projects/{cwd:path}/branches", response_model=BranchListResponse)
def api_list_branches(cwd: str, fetch: bool = False) -> BranchListResponse:
    try:
        return BranchListResponse.from_info(list_branches(cwd, fetch=fetch))
    except BranchError as e:
        # list_branches 对非 git 目录是降级返回而不是抛错，这里兜的是 git 命令本身失败
        return _error_response(e)


@router.post("/api/projects/{cwd:path}/checkout", response_model=BranchListResponse)
def api_checkout_branch(cwd: str, data: CheckoutRequest):
    try:
        return BranchListResponse.from_info(checkout(cwd, data.ref))
    except BranchError as e:
        return _error_response(e)
