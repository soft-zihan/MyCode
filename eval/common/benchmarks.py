from __future__ import annotations

import asyncio
import json
import random
from pathlib import Path
from typing import Any, Callable

from eval.common.executors import execute_task
from eval.common.models import BenchmarkName, EvalRunOptions, EvalRunState, EvalTask, EvalTaskResult
from eval.common.runner_base import (
    build_prompt,
    exact_match,
    extract_final_answer,
    gaia_question_scorer,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
GAIA_DATA_PATH = PROJECT_ROOT / "data" / "GAIA" / "all.json"
GAIA_FILES_DIR = PROJECT_ROOT / "data" / "GAIA" / "files"
HLE_DATA_PATH = PROJECT_ROOT / "data" / "HLE" / "all_500.json"
HLE_IMAGES_DIR = PROJECT_ROOT / "data" / "HLE" / "images"

EmitFn = Callable[[dict[str, Any]], None]


class BenchmarkAdapter:
    benchmark: BenchmarkName
    dataset_enabled = True

    def load_tasks(self, options: EvalRunOptions) -> list[EvalTask]:
        raise NotImplementedError

    async def setup(self, state: EvalRunState, options: EvalRunOptions) -> None:
        return None

    async def run_task(
        self,
        state: EvalRunState,
        task: EvalTask,
        result: EvalTaskResult,
        options: EvalRunOptions,
        index: int,
        total: int,
        emit: EmitFn,
    ) -> None:
        raise NotImplementedError

    async def teardown(self, state: EvalRunState, options: EvalRunOptions) -> None:
        return None


def select_raw_tasks(tasks: list[dict], options: EvalRunOptions, task_id_key: str) -> list[dict]:
    if options.only:
        wanted = list(options.only)
        by_id = {str(t.get(task_id_key) or t.get("id")): t for t in tasks}
        missing = [tid for tid in wanted if tid not in by_id]
        if missing:
            raise ValueError(f"未知任务: {missing}")
        return [by_id[tid] for tid in wanted]

    rng = random.Random(options.seed)
    if options.sample is None or options.sample >= len(tasks):
        selected = list(tasks)
        rng.shuffle(selected)
        return selected
    return rng.sample(tasks, options.sample)


class GaiaBenchmark(BenchmarkAdapter):
    benchmark: BenchmarkName = "gaia"

    def load_raw_tasks(self, level: int | None = None) -> list[dict[str, Any]]:
        tasks = json.loads(GAIA_DATA_PATH.read_text(encoding="utf-8"))
        if level:
            tasks = [task for task in tasks if task.get("Level") == level]
        return tasks

    def load_tasks(self, options: EvalRunOptions) -> list[EvalTask]:
        raw_tasks = select_raw_tasks(self.load_raw_tasks(options.level), options, "task_id")
        tasks: list[EvalTask] = []
        for raw in raw_tasks:
            task_id = str(raw.get("task_id") or raw.get("id"))
            question = raw["Question"]
            attachment = None
            file_name = raw.get("file_name")
            if file_name:
                candidate = GAIA_FILES_DIR / file_name
                attachment = str(candidate) if candidate.exists() else None
            tasks.append(EvalTask(
                task_id=task_id,
                benchmark=self.benchmark,
                name=f"L{raw.get('Level')} {question[:60]}",
                prompt=build_prompt(question, attachment=attachment),
                expected_output=str(raw.get("answer", "")),
                level=raw.get("Level"),
                metadata={
                    "question": question,
                    "question_preview": question[:150],
                    "file_name": file_name,
                    "attachment": attachment,
                    "attachment_missing": bool(file_name and not attachment),
                },
            ))
        return tasks

    async def run_task(
        self,
        state: EvalRunState,
        task: EvalTask,
        result: EvalTaskResult,
        options: EvalRunOptions,
        index: int,
        total: int,
        emit: EmitFn,
    ) -> None:
        out = await execute_task(task, state, options, state.model, state.api_base, state.api_key)
        result.predicted = extract_final_answer(out.get("text", ""))
        result.correct = (not out.get("error")) and gaia_question_scorer(task.expected_output, result.predicted)
        result.passed = result.correct
        result.status = task_status(out.get("error"), result.correct)
        result.duration_s = out.get("duration_s", 0.0)
        result.tokens = out.get("tokens", {})
        result.trace_id = out.get("trace_id")
        result.session_id = out.get("agent_session_id")
        result.error = out.get("error")
        result.metadata.update({
            "execution_mode": out.get("execution_mode"),
            "workspace": out.get("workspace"),
            "question_preview": task.metadata.get("question_preview"),
            "attachment_missing": task.metadata.get("attachment_missing"),
            "answer_text": out.get("text", "")[:1000],
        })


class HleBenchmark(GaiaBenchmark):
    benchmark: BenchmarkName = "hle"

    def load_raw_tasks(self, category: str | None = None, include_image: bool = False) -> list[dict[str, Any]]:
        tasks = json.loads(HLE_DATA_PATH.read_text(encoding="utf-8"))
        if not include_image:
            tasks = [task for task in tasks if not task.get("image")]
        if category:
            tasks = [task for task in tasks if task.get("category") == category]
        return tasks

    def load_tasks(self, options: EvalRunOptions) -> list[EvalTask]:
        raw_tasks = select_raw_tasks(self.load_raw_tasks(options.category, options.include_image), options, "id")
        tasks: list[EvalTask] = []
        for raw in raw_tasks:
            task_id = str(raw.get("id"))
            question = raw["question"]
            answer_type = raw.get("answer_type", "")
            attachment = None
            image = raw.get("image")
            if image:
                candidate = HLE_IMAGES_DIR / str(image)
                attachment = str(candidate) if candidate.exists() else None
            hint = "exact match (multiple choice letter)" if answer_type == "multiple-choice" else "exact match"
            tasks.append(EvalTask(
                task_id=task_id,
                benchmark=self.benchmark,
                name=question[:60],
                prompt=build_prompt(question, attachment=attachment, answer_hint=hint),
                expected_output=str(raw.get("answer", "")),
                category=raw.get("category"),
                metadata={
                    "question": question,
                    "question_preview": question[:150],
                    "answer_type": answer_type,
                    "image": image,
                    "attachment": attachment,
                    "attachment_missing": bool(image and not attachment),
                },
            ))
        return tasks

    async def run_task(
        self,
        state: EvalRunState,
        task: EvalTask,
        result: EvalTaskResult,
        options: EvalRunOptions,
        index: int,
        total: int,
        emit: EmitFn,
    ) -> None:
        out = await execute_task(task, state, options, state.model, state.api_base, state.api_key)
        result.predicted = extract_final_answer(out.get("text", ""))
        result.correct = (not out.get("error")) and exact_match(task.expected_output, result.predicted)
        result.passed = result.correct
        result.status = task_status(out.get("error"), result.correct)
        result.duration_s = out.get("duration_s", 0.0)
        result.tokens = out.get("tokens", {})
        result.trace_id = out.get("trace_id")
        result.session_id = out.get("agent_session_id")
        result.error = out.get("error")
        result.metadata.update({
            "execution_mode": out.get("execution_mode"),
            "workspace": out.get("workspace"),
            "question_preview": task.metadata.get("question_preview"),
            "answer_type": task.metadata.get("answer_type"),
            "category": task.category,
            "attachment_missing": task.metadata.get("attachment_missing"),
            "answer_text": out.get("text", "")[:1000],
        })


class SmokeBenchmark(BenchmarkAdapter):
    benchmark: BenchmarkName = "smoke"

    def __init__(self) -> None:
        self.listener: Any = None

    def load_tasks(self, options: EvalRunOptions) -> list[EvalTask]:
        from eval.smoke.runner import load_tasks

        raw_tasks = load_tasks(options.only, options.suite)
        if options.sample is not None:
            raw_tasks = raw_tasks[:max(0, options.sample)]
        tasks: list[EvalTask] = []
        for raw in raw_tasks:
            phases = raw.get("phases") or []
            messages = raw.get("messages") or [m for phase in phases for m in phase.get("messages", [])]
            expected = raw.get("expect") if not phases else [phase.get("expect", {}) for phase in phases]
            prompt = "\n".join(messages)
            tasks.append(EvalTask(
                task_id=str(raw["id"]),
                benchmark=self.benchmark,
                name=raw.get("name", raw["id"]),
                prompt=prompt,
                expected_output=json.dumps(expected, ensure_ascii=False),
                metadata={
                    "question": prompt,
                    "question_preview": prompt[:150],
                    "suite": options.suite,
                    "raw": raw,
                },
            ))
        return tasks

    async def setup(self, state: EvalRunState, options: EvalRunOptions) -> None:
        from eval.smoke.runner import EventListener, check_backend_health

        await asyncio.to_thread(check_backend_health, options.base_url)
        self.listener = EventListener(options.ws_url)
        await self.listener.start()

    async def run_task(
        self,
        state: EvalRunState,
        task: EvalTask,
        result: EvalTaskResult,
        options: EvalRunOptions,
        index: int,
        total: int,
        emit: EmitFn,
    ) -> None:
        from eval.smoke import runner as smoke_runner

        if self.listener is None:
            raise RuntimeError("SmokeBenchmark.setup 未执行")

        def relay(payload: dict[str, Any]) -> None:
            emit({"type": "eval/task_event", "run_id": state.run_id, "task_id": task.task_id, "payload": payload})

        record = await smoke_runner.run_task(
            self.listener,
            task.metadata["raw"],
            options.base_url,
            options.skip_langfuse,
            options.keep_sessions,
            on_event=relay,
        )
        result.passed = bool(record.get("passed"))
        result.correct = result.passed
        failures = record.get("failures", [])
        responses = record.get("responses") or []
        result.predicted = responses[-1].get("answer", "") if responses else ""
        result.status = "passed" if result.passed else ("error" if failures and failures[0].startswith("runner 异常") else "failed")
        result.duration_s = record.get("duration_s", 0.0)
        result.session_id = record.get("session_id")
        result.error = "; ".join(failures[:3]) if failures else None
        trace_ids = (record.get("langfuse") or {}).get("trace_ids") or []
        result.trace_id = trace_ids[0] if trace_ids else None
        result.metadata.update({
            "execution_mode": "smoke_backend_http",
            "workspace": record.get("workspace"),
            "turns": record.get("turns"),
            "failures": failures,
            "responses": responses,
            "langfuse": record.get("langfuse"),
            "bad_case_id": record.get("bad_case_id"),
            "trace_ids": trace_ids,
        })

    async def teardown(self, state: EvalRunState, options: EvalRunOptions) -> None:
        if self.listener is not None:
            await self.listener.stop()
            self.listener = None


def task_status(error: str | None, correct: bool | None) -> Any:
    if error and "aborted" in error:
        return "aborted"
    if error:
        return "error"
    return "passed" if correct else "failed"


def get_adapter(benchmark: BenchmarkName) -> BenchmarkAdapter:
    if benchmark == "gaia":
        return GaiaBenchmark()
    if benchmark == "hle":
        return HleBenchmark()
    if benchmark == "smoke":
        return SmokeBenchmark()
    raise ValueError(f"未知 benchmark: {benchmark}")


def summarize_results(results: list[EvalTaskResult]) -> dict[str, Any]:
    completed = [r for r in results if r.status not in ("pending", "running")]
    correct = sum(1 for r in completed if r.correct is True or r.passed is True)
    return {
        "total": len(results),
        "completed": len(completed),
        "correct": correct,
        "passed": correct,
        "failed": sum(1 for r in completed if r.status == "failed"),
        "errors": sum(1 for r in completed if r.status == "error"),
        "aborted": sum(1 for r in completed if r.status == "aborted"),
        "pass_at_1": round(correct / len(completed), 4) if completed else 0.0,
        "avg_duration_s": round(sum(r.duration_s for r in completed) / len(completed), 1) if completed else 0.0,
    }


def report_rows(state: EvalRunState) -> list[dict[str, Any]]:
    rows = []
    for result in state.tasks:
        row = result.to_dict()
        row["metadata"] = {
            k: v for k, v in result.metadata.items()
            if k not in {"raw", "responses", "langfuse"}
        }
        rows.append(row)
    return rows
