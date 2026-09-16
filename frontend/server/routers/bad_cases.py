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
    rewind_session_id: Optional[str]
    diagnosis: dict[str, Any]
    reproducible: Optional[bool]
    verification_diff: dict[str, Any]
    created_at: float
    updated_at: float


class UpdateBadCaseRequest(BaseModel):
    """更新 bad case 请求。"""
    status: Optional[str] = None
    severity: Optional[str] = None
    rewind_session_id: Optional[str] = None
    diagnosis: Optional[dict[str, Any]] = None
    reproducible: Optional[bool] = None
    verification_diff: Optional[dict[str, Any]] = None


class BadCaseListResponse(BaseModel):
    """Bad case 列表响应。"""
    items: list[BadCaseResponse]
    total: int


class VerifyRequest(BaseModel):
    """验证 bad case 可复现性请求。"""
    bad_case_id: str


class VerifyResponse(BaseModel):
    """验证结果响应。"""
    reproducible: bool
    severity: str
    message: str


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
        rewind_session_id=request.rewind_session_id,
        diagnosis=request.diagnosis,
        reproducible=request.reproducible,
        verification_diff=request.verification_diff,
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


# ── Rewind 集成 API ──


@router.post("/{bad_case_id}/verify", response_model=VerifyResponse)
async def api_verify_bad_case(bad_case_id: str) -> VerifyResponse:
    """验证 bad case 是否可复现。
    
    使用 Rewind 回放，对比结果判断是否偶发。
    """
    bad_case = get_bad_case(bad_case_id)
    if not bad_case:
        raise HTTPException(status_code=404, detail="Bad case not found")
    
    # 检查 Rewind 是否启用
    from agents.observability.rewind import is_enabled, get_session_id, diagnose_failure, init_rewind
    if not is_enabled():
        return VerifyResponse(
            reproducible=False,
            severity="unknown",
            message="Rewind 未启用，无法验证可复现性",
        )
    
    # 初始化 Rewind（如果尚未初始化）
    init_rewind()
    
    # 获取当前 Rewind session
    rewind_session_id = get_session_id()
    
    # 诊断失败原因
    diagnosis = diagnose_failure(rewind_session_id)
    
    # 简化判断：如果有诊断结果，认为可复现
    reproducible = diagnosis is not None
    severity = "high" if reproducible else "low"
    
    # 更新 bad case
    update_bad_case(
        bad_case_id=bad_case_id,
        rewind_session_id=rewind_session_id,
        diagnosis=diagnosis or {},
        reproducible=reproducible,
        status=BadCaseStatus.VERIFIED if reproducible else BadCaseStatus.FLAKY,
    )
    
    return VerifyResponse(
        reproducible=reproducible,
        severity=severity,
        message="已验证" if reproducible else "偶发问题或无法诊断",
    )


@router.post("/{bad_case_id}/export-rewind")
def api_export_rewind(bad_case_id: str) -> dict[str, Any]:
    """导出 bad case 的 Rewind session。"""
    bad_case = get_bad_case(bad_case_id)
    if not bad_case:
        raise HTTPException(status_code=404, detail="Bad case not found")
    
    from agents.observability.rewind import is_enabled, export_session
    if not is_enabled():
        raise HTTPException(status_code=400, detail="Rewind 未启用")
    
    from pathlib import Path
    export_dir = Path.home() / ".mycode" / "bad-cases" / bad_case_id
    export_dir.mkdir(parents=True, exist_ok=True)
    
    success = export_session(output_dir=str(export_dir))
    if not success:
        raise HTTPException(status_code=500, detail="导出失败")
    
    # 更新 bad case
    update_bad_case(
        bad_case_id=bad_case_id,
        rewind_session_id=bad_case.session_id,
    )
    
    return {"success": True, "export_dir": str(export_dir)}


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
        rewind_session_id=bad_case.rewind_session_id,
        diagnosis=bad_case.diagnosis,
        reproducible=bad_case.reproducible,
        verification_diff=bad_case.verification_diff,
        created_at=bad_case.created_at,
        updated_at=bad_case.updated_at,
    )
