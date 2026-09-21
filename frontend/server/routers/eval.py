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
        "name": "GAIA (Level 3)",
        "description": "通用 AI 助手基准，只保留最难的 Level 3（媒体过滤后 22 题）：文件解析、多步推理、工具使用。精确匹配 pass@1。",
        "detail": {
            "purpose": "衡量通用助手能力：文件解析、多步推理、网页检索、工具编排。Level 3 是最难档，长上下文多步推理密集，最能区分 agent 能力与压缩策略代价（compression-arm 消融实验的标准载体）。",
            "data_source": "data/GAIA/all.json（官方验证集 165 题；固定 Level 3，默认过滤媒体依赖题后 22 题，部分题带附件文件）",
            "scoring": "精确匹配 pass@1（数字/字符串归一化比较），可叠加 judge 评审",
            "execution": "backend_session（真实后端 HTTP+WebSocket 会话）或 in_process（进程内直调 Agent）",
            "duration": "单题约 5-15 分钟；全量 22 题约 2-4 小时",
            "commands": [
                "python -m eval.gaia.runner --sample 10 --seed 42",
                "python -m eval.gaia.runner --sample 10 --seed 42 --compression-arm none --thinking off",
            ],
        },
        "execution_modes": ["backend_session", "in_process"],
        "default_options": {"sample": 5, "seed": 42, "timeout_s": 0, "execution_mode": "backend_session"},
    },
    {
        "id": "hle",
        "name": "HLE (Humanity's Last Exam)",
        "description": "极高难度多学科闭卷问答，考验前沿模型推理上限。精确匹配 pass@1。",
        "detail": {
            "purpose": "用极高难度多学科闭卷题衡量模型推理上限；无工具、无附件，适合对比 thinking 开关与不同模型的裸推理能力。",
            "data_source": "data/HLE/all_500.json（500 题子集；默认过滤图片题，可按 category 过滤）",
            "scoring": "精确匹配 pass@1（选择题按选项字母比较）",
            "execution": "backend_session 或 in_process",
            "duration": "单题约 1-5 分钟",
            "commands": [
                "python -m eval.hle.runner --sample 10 --seed 42",
            ],
        },
        "execution_modes": ["backend_session", "in_process"],
        "default_options": {"sample": 10, "seed": 42, "timeout_s": 0, "execution_mode": "backend_session"},
    },
    {
        "id": "smoke",
        "name": "Smoke / Chain（本项目端到端）",
        "description": "本项目自身的端到端评测（真实后端 HTTP + WebSocket）：smoke 单元冒烟 + chain 全链路案例。发布或大改后必跑。",
        "detail": {
            "purpose": "守护本项目自身功能链路：上下文压缩、错误恢复、多代理协作、Wiki 编译召回、skill 编译、remember 闭环、电商全链路。每个案例独立会话，可勾选单跑。",
            "data_source": "eval/smoke/conversations.jsonl（单元冒烟 16 例）、eval/smoke/chain.jsonl（全链路 7 例）",
            "scoring": "逐案例断言：文件产物 / 工具调用 / 事件流 / 关键词检查，pass/fail",
            "execution": "固定 backend_session（真实 HTTP+WS，走完整后端链路）",
            "duration": "smoke 全套约 3-5 分钟；chain 全套约 15-25 分钟（含长任务案例）",
            "commands": [
                "python -m eval.smoke.runner --suite chain",
                "python -m eval.smoke.runner --suite smoke --only read_file shell_exec",
            ],
        },
        "execution_modes": ["smoke_backend_http"],
        "default_options": {"suite": "chain", "timeout_s": 0, "keep_sessions": True},
        "suites": [
            {"id": "chain", "name": "chain（全链路评测）", "description": "7 个端到端案例：context_stress（压缩）、error_recovery（工具错误反馈）、multi_agent（子代理协作）、wiki_compile_and_recall（折叠→提取→召回）、workflow_skill_compile（模式→skill 编译）、remember_feedback_loop（feedback 写入+同会话召回）、ecommerce_full_chain（教学→编译→新会话召回→citation 不泄漏 一体案例）。"},
            {"id": "smoke", "name": "smoke（单元冒烟）", "description": "基础能力单测：文件读写、shell、搜索、多轮上下文等最小用例，快速验证后端无回归。"},
        ],
    },
]


class EvalRunRequest(BaseModel):
    benchmark: Literal["gaia", "hle", "smoke"]
    sample: int | None = None
    seed: int = 42
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
    compression_arm: Literal["none", "tool_only", "session_only", "full"] | None = None


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


@router.get("/tasks")
async def api_eval_tasks(benchmark: str, suite: str = "chain") -> dict[str, Any]:
    """列出评测集任务（用于 UI 浏览与单任务直跑）。"""
    if benchmark == "smoke":
        from eval.smoke.runner import load_tasks

        if suite not in ("smoke", "chain"):
            raise HTTPException(status_code=404, detail=f"unknown suite: {suite}")
        raw = load_tasks(suite=suite)
        items = [{"id": t["id"], "name": t.get("name", t["id"]), "meta": {}} for t in raw]
    elif benchmark == "gaia":
        from eval.common.benchmarks import GaiaBenchmark

        raw = GaiaBenchmark().load_raw_tasks(include_media=False)
        items = [{
            "id": str(t.get("task_id") or t.get("id")),
            "name": str(t.get("Question", ""))[:100],
            "meta": {"file_name": t.get("file_name") or ""},
        } for t in raw]
    elif benchmark == "hle":
        from eval.common.benchmarks import HleBenchmark

        raw = HleBenchmark().load_raw_tasks(include_image=False)
        items = [{
            "id": str(t.get("id")),
            "name": str(t.get("question", ""))[:100],
            "meta": {"category": t.get("category") or ""},
        } for t in raw]
    else:
        raise HTTPException(status_code=404, detail=f"unknown benchmark: {benchmark}")
    return {
        "benchmark": benchmark,
        "suite": suite if benchmark == "smoke" else None,
        "total": len(items),
        "tasks": items,
    }


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
