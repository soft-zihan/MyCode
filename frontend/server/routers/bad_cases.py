"""Bad Case 墙 API。

提供 bad case 的增删改查接口，以及用户反馈（踩按钮）接口。
"""

from __future__ import annotations

import time
import uuid
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from agents.observability.bad_cases import (
    BadCase,
    BadCaseSource,
    BadCaseStatus,
    BadCaseSeverity,
    create_bad_case,
    get_bad_case,
    list_bad_cases,
    update_bad_case,
    delete_bad_case,
    count_bad_cases,
)

router = APIRouter(prefix="/api/bad-cases", tags=["bad-cases"])


# ── 请求/响应模型 ──


class ThumbsDownRequest(BaseModel):
    """踩按钮请求。"""
    session_id: str
    turn_number: int
    step_number: Optional[int] = None
    tool_name: Optional[str] = None
    reason: str  # wrong_tool, incomplete, memory_not_used, other
    expected_tool: Optional[str] = None
    comment: Optional[str] = None


class BadCaseResponse(BaseModel):
    """Bad case 响应。"""
    id: str
    session_id: str
    source: str
    status: str
    severity: str
    turn_number: Optional[int]
    step_number: Optional[int]
    tool_name: Optional[str]
    signal_type: str
    reason: str
    comment: str
    expected_tool: Optional[str]
    created_at: float
    updated_at: float


class UpdateBadCaseRequest(BaseModel):
    """更新 bad case 请求。"""
    status: Optional[str] = None
    severity: Optional[str] = None


class BadCaseListResponse(BaseModel):
    """Bad case 列表响应。"""
    items: list[BadCaseResponse]
    total: int


# ── 用户反馈 API ──


@router.post("/feedback", response_model=BadCaseResponse)
def api_thumbs_down(request: ThumbsDownRequest) -> BadCaseResponse:
    """用户踩按钮反馈。
    
    每条 assistant message 旁边都有踩按钮，用户点击后提交反馈。
    """
    bad_case = BadCase(
        id=uuid.uuid4().hex[:8],
        session_id=request.session_id,
        source=BadCaseSource.USER_FEEDBACK,
        status=BadCaseStatus.PENDING,
        severity=BadCaseSeverity.MEDIUM,
        turn_number=request.turn_number,
        step_number=request.step_number,
        tool_name=request.tool_name,
        signal_type=request.reason,
        reason=request.reason,
        comment=request.comment or "",
        expected_tool=request.expected_tool,
        created_at=time.time(),
        updated_at=time.time(),
    )
    
    created = create_bad_case(bad_case)
    return _to_response(created)


# ── Bad Case 墙 API ──


@router.get("", response_model=BadCaseListResponse)
def api_list_bad_cases(
    session_id: Optional[str] = None,
    source: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 100,
) -> BadCaseListResponse:
    """列出 bad cases。"""
    source_enum = BadCaseSource(source) if source else None
    status_enum = BadCaseStatus(status) if status else None
    
    items = list_bad_cases(
        session_id=session_id,
        source=source_enum,
        status=status_enum,
        limit=limit,
    )
    
    total = count_bad_cases(source=source_enum, status=status_enum)
    
    return BadCaseListResponse(
        items=[_to_response(item) for item in items],
        total=total,
    )


@router.get("/{bad_case_id}", response_model=BadCaseResponse)
def api_get_bad_case(bad_case_id: str) -> BadCaseResponse:
    """获取单个 bad case。"""
    bad_case = get_bad_case(bad_case_id)
    if not bad_case:
        raise HTTPException(status_code=404, detail="Bad case not found")
    return _to_response(bad_case)


@router.patch("/{bad_case_id}", response_model=BadCaseResponse)
def api_update_bad_case(bad_case_id: str, request: UpdateBadCaseRequest) -> BadCaseResponse:
    """更新 bad case。"""
    status_enum = BadCaseStatus(request.status) if request.status else None
    severity_enum = BadCaseSeverity(request.severity) if request.severity else None
    
    updated = update_bad_case(
        bad_case_id=bad_case_id,
        status=status_enum,
        severity=severity_enum,
    )
    
    if not updated:
        raise HTTPException(status_code=404, detail="Bad case not found")
    
    return _to_response(updated)


@router.delete("/{bad_case_id}")
def api_delete_bad_case(bad_case_id: str) -> dict[str, bool]:
    """删除 bad case。"""
    success = delete_bad_case(bad_case_id)
    if not success:
        raise HTTPException(status_code=404, detail="Bad case not found")
    return {"success": True}


# ── 统计 API ──


@router.get("/stats/summary")
def api_bad_case_stats() -> dict[str, Any]:
    """Bad case 统计摘要。"""
    return {
        "total": count_bad_cases(),
        "pending": count_bad_cases(status=BadCaseStatus.PENDING),
        "verified": count_bad_cases(status=BadCaseStatus.VERIFIED),
        "fixed": count_bad_cases(status=BadCaseStatus.FIXED),
        "flaky": count_bad_cases(status=BadCaseStatus.FLAKY),
        "ignored": count_bad_cases(status=BadCaseStatus.IGNORED),
        "by_source": {
            "user_feedback": count_bad_cases(source=BadCaseSource.USER_FEEDBACK),
            "auto_detect": count_bad_cases(source=BadCaseSource.AUTO_DETECT),
            "eval": count_bad_cases(source=BadCaseSource.EVAL),
        },
    }


# ── 辅助函数 ──


def _to_response(bad_case: BadCase) -> BadCaseResponse:
    """将 BadCase 转换为响应模型。"""
    return BadCaseResponse(
        id=bad_case.id,
        session_id=bad_case.session_id,
        source=bad_case.source.value,
        status=bad_case.status.value,
        severity=bad_case.severity.value,
        turn_number=bad_case.turn_number,
        step_number=bad_case.step_number,
        tool_name=bad_case.tool_name,
        signal_type=bad_case.signal_type,
        reason=bad_case.reason,
        comment=bad_case.comment,
        expected_tool=bad_case.expected_tool,
        created_at=bad_case.created_at,
        updated_at=bad_case.updated_at,
    )
