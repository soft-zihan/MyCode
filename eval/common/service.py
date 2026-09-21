from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from eval.common.benchmarks import get_adapter, report_rows, summarize_results
from eval.common.langfuse_dataset import dataset_name, sync_task_to_dataset
from eval.common.models import (
    BenchmarkName,
    EvalRunOptions,
    EvalRunState,
    EvalTaskResult,
)
from eval.common.runner_base import REPORTS_DIR, init_eval_tracing, resolve_model_config, write_reports

logger = logging.getLogger(__name__)
EmitFn = Callable[[dict[str, Any]], None]


def new_run_id(benchmark: BenchmarkName) -> str:
    return f"{benchmark}-{time.strftime('%Y%m%d_%H%M%S')}-{uuid.uuid4().hex[:6]}"


def state_path(run_id: str) -> Path:
    return REPORTS_DIR / f"{run_id}.state.json"


def report_json_path(run_id: str) -> Path:
    return REPORTS_DIR / f"{run_id}.json"


def report_md_path(run_id: str) -> Path:
    return REPORTS_DIR / f"{run_id}.md"


def save_state(state: EvalRunState) -> Path:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    path = state_path(state.run_id)
    path.write_text(json.dumps(state.to_dict(), ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return path


def load_state(run_id: str) -> dict[str, Any] | None:
    path = state_path(run_id)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def load_report(run_id: str) -> dict[str, Any] | None:
    path = report_json_path(run_id)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def list_report_runs(limit: int = 100) -> list[dict[str, Any]]:
    if not REPORTS_DIR.exists():
        return []
    runs: list[dict[str, Any]] = []
    for path in sorted(REPORTS_DIR.glob("*.state.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning("读取 eval run 状态失败 %s: %s", path, e)
            continue
        runs.append({
            "run_id": data.get("run_id"),
            "benchmark": data.get("benchmark"),
            "status": data.get("status"),
            "created_at": data.get("created_at"),
            "started_at": data.get("started_at"),
            "finished_at": data.get("finished_at"),
            "model": data.get("model"),
            "eval_session_id": data.get("eval_session_id"),
            "summary": data.get("summary", {}),
            "report_json_path": data.get("report_json_path"),
            "report_md_path": data.get("report_md_path"),
            "error": data.get("error"),
        })
    return runs


class EvalService:
    def __init__(self) -> None:
        self.runs: dict[str, EvalRunState] = {}
        self.tasks: dict[str, asyncio.Task[dict[str, Any]]] = {}
        self.emitters: dict[str, list[EmitFn]] = {}
        self._cancel_requested: set[str] = set()

    def emit(self, run_id: str, payload: dict[str, Any]) -> None:
        payload = {"run_id": run_id, **payload}
        for emitter in self.emitters.get(run_id, []):
            try:
                emitter(payload)
            except Exception as e:
                logger.warning("eval event emitter failed run=%s: %s", run_id, e)

    def attach_emitter(self, run_id: str, emitter: EmitFn) -> Callable[[], None]:
        self.emitters.setdefault(run_id, []).append(emitter)

        def detach() -> None:
            emitters = self.emitters.get(run_id)
            if emitters and emitter in emitters:
                emitters.remove(emitter)

        return detach

    def create_state(self, options: EvalRunOptions) -> EvalRunState:
        run_id = new_run_id(options.benchmark)
        state = EvalRunState(
            run_id=run_id,
            benchmark=options.benchmark,
            eval_session_id=f"eval-{run_id}",
            options=options,
        )
        self.runs[run_id] = state
        return state

    def start_run(self, options: EvalRunOptions, emitter: EmitFn | None = None) -> EvalRunState:
        state = self.create_state(options)
        if emitter:
            self.attach_emitter(state.run_id, emitter)
        save_state(state)
        task = asyncio.create_task(self.run(state, options))
        self.tasks[state.run_id] = task
        return state

    async def run(self, state: EvalRunState, options: EvalRunOptions | None = None) -> dict[str, Any]:
        options = options or state.options
        adapter = get_adapter(options.benchmark)
        state.status = "running"
        state.started_at = time.time()
        state.options = options

        if options.benchmark == "smoke":
            state.model = "backend-service"
            state.api_base = options.base_url
        else:
            if not options.skip_langfuse:
                init_eval_tracing(options.benchmark)
            state.model, state.api_base, state.api_key = resolve_model_config(options.model, options.api_base)

        langfuse_client: Any = None
        if options.sync_langfuse_dataset and not options.skip_langfuse:
            langfuse_client = await asyncio.to_thread(self._create_langfuse_client)

        try:
            raw_tasks = await asyncio.to_thread(adapter.load_tasks, options)
            state.tasks = [
                EvalTaskResult(
                    task_id=task.task_id,
                    benchmark=task.benchmark,
                    name=task.name,
                    expected=task.expected_output,
                    metadata={
                        "level": task.level,
                        "category": task.category,
                        "question_preview": task.metadata.get("question_preview"),
                    },
                )
                for task in raw_tasks
            ]
            save_state(state)
            self.emit(state.run_id, {
                "type": "eval/run_started",
                "benchmark": options.benchmark,
                "model": state.model,
                "eval_session_id": state.eval_session_id,
                "total": len(raw_tasks),
                "options": options.to_dict(),
            })

            await adapter.setup(state, options)
            total = len(raw_tasks)
            for index, (task, result) in enumerate(zip(raw_tasks, state.tasks), 1):
                if state.run_id in self._cancel_requested:
                    result.status = "aborted"
                    continue

                result.status = "running"
                save_state(state)
                self.emit(state.run_id, {
                    "type": "eval/task_started",
                    "task_id": task.task_id,
                    "index": index,
                    "total": total,
                    "name": task.name,
                })

                try:
                    await adapter.run_task(state, task, result, options, index, total, lambda payload: self.emit(state.run_id, payload))
                except asyncio.CancelledError:
                    result.status = "aborted"
                    result.error = result.error or "aborted"
                    self._cancel_requested.add(state.run_id)
                    raise
                except Exception as e:
                    result.status = "error"
                    result.error = f"{type(e).__name__}: {e}"
                    logger.exception("eval task failed run=%s task=%s", state.run_id, task.task_id)

                if langfuse_client is not None and result.trace_id:
                    try:
                        result.langfuse_dataset = await asyncio.to_thread(
                            sync_task_to_dataset,
                            langfuse_client,
                            task,
                            result,
                            state,
                            options,
                        )
                    except Exception as e:
                        result.langfuse_dataset = {"synced": False, "errors": [f"unexpected: {type(e).__name__}: {e}"]}
                        logger.warning("Langfuse dataset sync failed run=%s task=%s: %s", state.run_id, task.task_id, e)

                state.summary = summarize_results(state.tasks)
                save_state(state)
                self.emit(state.run_id, {
                    "type": "eval/task_finished",
                    "task_id": task.task_id,
                    "index": index,
                    "total": total,
                    "result": result.to_dict(),
                })
                self.emit(state.run_id, {
                    "type": "eval/run_progress",
                    "summary": state.summary,
                })

            await adapter.teardown(state, options)
            state.summary = summarize_results(state.tasks)
            state.status = "aborted" if state.run_id in self._cancel_requested else "completed"
        except asyncio.CancelledError:
            await adapter.teardown(state, options)
            state.status = "aborted"
            state.error = "aborted"
            raise
        except Exception as e:
            await adapter.teardown(state, options)
            state.status = "failed"
            state.error = f"{type(e).__name__}: {e}"
            logger.exception("eval run failed run=%s", state.run_id)
            self.emit(state.run_id, {"type": "eval/run_error", "error": state.error})
        finally:
            self._cancel_requested.discard(state.run_id)
            state.finished_at = time.time()
            state.summary = summarize_results(state.tasks)
            try:
                state.report_json_path, state.report_md_path = self.write_report(state, options)
            except Exception as e:
                state.error = f"{state.error}; " if state.error else ""
                state.error += f"report: {type(e).__name__}: {e}"
                logger.exception("eval report failed run=%s", state.run_id)
            if options.judge_after_run and state.status != "failed":
                try:
                    await self.judge_run(state.run_id)
                except Exception as e:
                    logger.warning("judge after run failed run=%s: %s", state.run_id, e)
                    self.emit(state.run_id, {"type": "eval/judge_error", "error": f"{type(e).__name__}: {e}"})
            save_state(state)
            self.emit(state.run_id, {
                "type": "eval/run_finished",
                "status": state.status,
                "summary": state.summary,
                "report_json_path": state.report_json_path,
                "report_md_path": state.report_md_path,
                "error": state.error,
            })

        return state.to_dict()

    def write_report(self, state: EvalRunState, options: EvalRunOptions) -> tuple[str, str]:
        rows = report_rows(state)
        meta = {
            "model": state.model,
            "seed": options.seed,
            "level": options.level,
            "category": options.category,
            "include_image": options.include_image,
            "suite": options.suite,
            "session_id": state.eval_session_id,
            "timeout_s": options.timeout_s,
            "execution_mode": options.execution_mode,
            "sync_langfuse_dataset": options.sync_langfuse_dataset,
            "langfuse_dataset": dataset_name(options.benchmark) if options.sync_langfuse_dataset else None,
            "status": state.status,
        }
        json_path, md_path = write_reports(
            options.benchmark,
            state.run_id,
            meta,
            state.summary,
            rows,
            reports_dir=REPORTS_DIR,
            report_stem=state.run_id,
        )
        return str(json_path), str(md_path)

    def get_state(self, run_id: str) -> EvalRunState | dict[str, Any] | None:
        return self.runs.get(run_id) or load_state(run_id)

    def get_run_dict(self, run_id: str) -> dict[str, Any] | None:
        state = self.runs.get(run_id)
        if state:
            return state.to_dict()
        return load_state(run_id) or load_report(run_id)

    def abort_run(self, run_id: str) -> bool:
        task = self.tasks.get(run_id)
        state = self.runs.get(run_id)
        if not task or task.done():
            if state and state.status == "running":
                state.status = "aborted"
                state.finished_at = time.time()
                save_state(state)
                self.emit(run_id, {"type": "eval/run_finished", "status": "aborted", "summary": state.summary})
                return True
            return False

        self._cancel_requested.add(run_id)
        task.cancel()
        return True

    async def judge_run(self, run_id: str, judge: bool = True) -> dict[str, Any]:
        from agents.observability.langfuse_api import get_client
        from eval.langfuse.pipeline import evaluate_traces

        run = self.get_run_dict(run_id)
        if not run:
            raise ValueError(f"未知 run_id: {run_id}")
        trace_ids = [
            task["trace_id"]
            for task in run.get("tasks", [])
            if task.get("trace_id")
        ]
        if not trace_ids:
            return {"run_id": run_id, "traces": 0, "code_scores": 0, "results": []}

        client = get_client()
        self.emit(run_id, {"type": "eval/judge_started", "traces": len(trace_ids), "judge": judge})

        def progress(payload: dict[str, Any]) -> None:
            self.emit(run_id, {"type": "eval/judge_progress", **payload})

        summary = await asyncio.to_thread(evaluate_traces, client, trace_ids, judge=judge, progress=progress)
        self.emit(run_id, {"type": "eval/judge_finished", **{k: v for k, v in summary.items() if k != "results"}})
        return summary

    def _create_langfuse_client(self) -> Any:
        from agents.observability.langfuse_api import get_client

        return get_client()


_service: EvalService | None = None


def get_eval_service() -> EvalService:
    global _service
    if _service is None:
        _service = EvalService()
    return _service
