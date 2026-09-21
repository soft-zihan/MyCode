from __future__ import annotations

import asyncio
from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from .websocket import broadcast_event

router = APIRouter(prefix="/api/eval", tags=["eval"])

BENCHMARKS = [
    {
        "id": "gaia",
        "name": "GAIA",
        "description": "General AI Assistants benchmark",
        "execution_modes": ["backend_session", "in_process"],
        "default_options": {"sample": 5, "seed": 42, "timeout_s": 0, "execution_mode": "backend_session"},
    },
    {
        "id": "hle",
        "name": "HLE",
        "description": "Humanity's Last Exam",
        "execution_modes": ["backend_session", "in_process"],
        "default_options": {"sample": 10, "seed": 42, "timeout_s": 0, "execution_mode": "backend_session"},
    },
    {
        "id": "smoke",
        "name": "Smoke / Chain",
        "description": "Project end-to-end benchmark through real backend HTTP + WebSocket",
        "execution_modes": ["smoke_backend_http"],
        "default_options": {"suite": "chain", "timeout_s": 0, "keep_sessions": True},
    },
]


class EvalRunRequest(BaseModel):
    benchmark: Literal["gaia", "hle", "smoke"]
    sample: int | None = None
    seed: int = 42
    level: int | None = Field(default=None, ge=1, le=3)
    category: str | None = None
    include_image: bool = False
    only: list[str] | None = None
    suite: str = "chain"
    timeout_s: int = 0
    model: str | None = None
    api_base: str | None = None
    execution_mode: Literal["backend_session", "in_process"] = "backend_session"
    sync_langfuse_dataset: bool = True
    judge_after_run: bool = False
    skip_langfuse: bool = False
    keep_sessions: bool = True
    base_url: str = "http://localhost:5555"
    ws_url: str = "ws://localhost:5555/ws/events"
    thinking: bool | None = None


class JudgeRequest(BaseModel):
    judge: bool = True


def _options_from_request(data: EvalRunRequest) -> Any:
    from eval.common.models import EvalRunOptions

    return EvalRunOptions(**data.model_dump())


def _broadcast(payload: dict[str, Any]) -> None:
    broadcast_event({"session_id": None, **payload})


@router.get("/benchmarks")
async def api_eval_benchmarks() -> dict[str, Any]:
    return {"benchmarks": BENCHMARKS}


@router.get("/langfuse")
async def api_eval_langfuse() -> dict[str, Any]:
    from agents.observability.langfuse_api import get_client

    try:
        client = get_client()
        project = await asyncio.to_thread(client.fetch_project)
        return {
            "configured": True,
            "base_url": client.base_url,
            "project_id": project.get("id"),
            "project_name": project.get("name"),
        }
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Langfuse 不可用: {type(e).__name__}: {e}")


@router.get("/runs")
async def api_eval_runs(limit: int = 100) -> dict[str, Any]:
    from eval.common.service import get_eval_service, list_report_runs

    service = get_eval_service()
    active = [state.to_dict() for state in service.runs.values() if state.status in ("pending", "running")]
    return {"active": active, "runs": list_report_runs(limit)}


@router.post("/runs")
async def api_eval_start_run(data: EvalRunRequest) -> dict[str, Any]:
    from eval.common.service import get_eval_service

    service = get_eval_service()
    options = _options_from_request(data)
    if options.benchmark == "smoke":
        options.execution_mode = "backend_session"
    state = service.start_run(options, emitter=_broadcast)
    return state.to_dict()


@router.get("/runs/{run_id}")
async def api_eval_run(run_id: str) -> dict[str, Any]:
    from eval.common.service import get_eval_service

    service = get_eval_service()
    run = service.get_run_dict(run_id)
    if not run:
        raise HTTPException(status_code=404, detail=f"未知 run_id: {run_id}")
    return run


@router.post("/runs/{run_id}/abort")
async def api_eval_abort_run(run_id: str) -> dict[str, Any]:
    from eval.common.service import get_eval_service

    service = get_eval_service()
    aborted = service.abort_run(run_id)
    if not aborted:
        raise HTTPException(status_code=404, detail=f"没有可中止的 run: {run_id}")
    return {"run_id": run_id, "aborted": True}


@router.post("/runs/{run_id}/judge")
async def api_eval_judge_run(run_id: str, data: JudgeRequest) -> dict[str, Any]:
    from eval.common.service import get_eval_service

    service = get_eval_service()
    try:
        return await service.judge_run(run_id, judge=data.judge)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {e}")


@router.get("/reports")
async def api_eval_reports(limit: int = 100) -> dict[str, Any]:
    from eval.common.service import list_report_runs

    return {"runs": list_report_runs(limit)}


@router.get("/reports/{run_id}")
async def api_eval_report(run_id: str) -> dict[str, Any]:
    from eval.common.service import load_report, load_state

    report = load_report(run_id)
    if report:
        return report
    state = load_state(run_id)
    if state:
        return state
    raise HTTPException(status_code=404, detail=f"未知报告: {run_id}")


@router.get("/reports/{run_id}/markdown", response_class=PlainTextResponse)
async def api_eval_report_markdown(run_id: str) -> str:
    from eval.common.service import report_md_path

    path = report_md_path(run_id)
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"未知 Markdown 报告: {run_id}")
    return path.read_text(encoding="utf-8")
