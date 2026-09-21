from __future__ import annotations

import logging
from typing import Any

from eval.common.models import EvalRunOptions, EvalRunState, EvalTask, EvalTaskResult

logger = logging.getLogger(__name__)

DATASET_NAMES = {
    "gaia": "gaia-eval",
    "hle": "hle-eval",
    "smoke": "smoke-eval",
}


def dataset_name(benchmark: str) -> str:
    return DATASET_NAMES.get(benchmark, f"{benchmark}-eval")


def dataset_item_id(benchmark: str, task_id: str) -> str:
    return f"{benchmark}-{task_id}"


def dataset_item_input(task: EvalTask) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "task_id": task.task_id,
        "benchmark": task.benchmark,
        "question": task.metadata.get("question") or task.prompt,
        "prompt": task.prompt,
    }
    if task.name:
        payload["name"] = task.name
    if task.level is not None:
        payload["level"] = task.level
    if task.category:
        payload["category"] = task.category
    if task.metadata.get("attachment"):
        payload["attachment"] = task.metadata["attachment"]
    return payload


def dataset_item_metadata(task: EvalTask, result: EvalTaskResult, state: EvalRunState, options: EvalRunOptions) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "benchmark": task.benchmark,
        "task_id": task.task_id,
        "run_id": state.run_id,
        "eval_session_id": state.eval_session_id,
        "agent_session_id": result.session_id,
        "model": state.model,
        "seed": options.seed,
        "timeout_s": options.timeout_s,
        "execution_mode": options.execution_mode,
        "expected_output": task.expected_output,
        "task_metadata": task.metadata,
    }
    if task.level is not None:
        metadata["level"] = task.level
    if task.category:
        metadata["category"] = task.category
    return metadata


def sync_task_to_dataset(
    client: Any,
    task: EvalTask,
    result: EvalTaskResult,
    state: EvalRunState,
    options: EvalRunOptions,
) -> dict[str, Any]:
    name = dataset_name(task.benchmark)
    item_id = dataset_item_id(task.benchmark, task.task_id)
    status: dict[str, Any] = {
        "dataset": name,
        "item_id": item_id,
        "run_name": state.run_id,
        "synced": False,
        "scored": False,
        "errors": [],
    }

    try:
        client.create_dataset(name, description=f"{task.benchmark.upper()} benchmark dataset")
        item = client.upsert_dataset_item(
            dataset_name=name,
            input=dataset_item_input(task),
            expected_output={"answer": task.expected_output},
            metadata=dataset_item_metadata(task, result, state, options),
            item_id=item_id,
            source_trace_id=result.trace_id,
        )
        status["item_id"] = item.get("id", item_id)
        status["synced"] = True
    except Exception as e:
        status["errors"].append(f"dataset_item: {type(e).__name__}: {e}")
        logger.warning("Langfuse dataset item sync failed benchmark=%s task=%s: %s", task.benchmark, task.task_id, e)

    if status["synced"]:
        try:
            run_item = client.create_dataset_run_item(
                run_name=state.run_id,
                dataset_item_id=status["item_id"],
                trace_id=result.trace_id,
                run_description=f"{task.benchmark} eval run {state.run_id}",
                metadata=dataset_item_metadata(task, result, state, options),
            )
            status["dataset_run_item_id"] = run_item.get("id")
            status["dataset_run"] = state.run_id
        except Exception as e:
            status["errors"].append(f"dataset_run_item: {type(e).__name__}: {e}")
            logger.warning("Langfuse dataset run item failed benchmark=%s task=%s: %s", task.benchmark, task.task_id, e)

    value = result.correct if result.correct is not None else result.passed
    if result.trace_id and value is not None:
        try:
            client.create_score(
                trace_id=result.trace_id,
                name="benchmark_correct",
                value=bool(value),
                data_type="BOOLEAN",
                comment=result.error or f"expected={result.expected[:100]} predicted={result.predicted[:100]}",
            )
            status["scored"] = True
        except Exception as e:
            status["errors"].append(f"score: {type(e).__name__}: {e}")
            logger.warning("Langfuse benchmark score failed benchmark=%s task=%s: %s", task.benchmark, task.task_id, e)

    return status
