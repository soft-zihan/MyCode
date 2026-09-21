from __future__ import annotations

import asyncio
from typing import Any

from eval.common.models import EvalRunOptions
from eval.common.service import EvalService


def print_event(payload: dict[str, Any]) -> None:
    event_type = payload.get("type")
    run_id = payload.get("run_id", "")

    if event_type == "eval/run_started":
        print(f"[{payload.get('benchmark')}] run_id={run_id} model={payload.get('model')} tasks={payload.get('total')}")
    elif event_type == "eval/task_started":
        print(f"  [{payload.get('index')}/{payload.get('total')}] {str(payload.get('task_id'))[:12]}… ", end="", flush=True)
    elif event_type == "eval/task_finished":
        result = payload.get("result", {})
        mark = "✅" if result.get("status") == "passed" else "❌"
        expected = str(result.get("expected", ""))[:30]
        predicted = str(result.get("predicted", ""))[:30]
        print(f"{mark} {result.get('duration_s', 0)}s expected={expected!r} predicted={predicted!r}")
        if result.get("error"):
            print(f"      error: {result['error']}")
    elif event_type == "eval/run_finished":
        summary = payload.get("summary", {})
        correct = summary.get("correct", summary.get("passed", 0))
        total = summary.get("total", 0)
        pass_at_1 = summary.get("pass_at_1", 0.0)
        print(f"\n[eval] {payload.get('status')} {correct}/{total} = {pass_at_1:.1%}")
        print(f"[eval] report: {payload.get('report_json_path')}")
        print(f"[eval]         {payload.get('report_md_path')}")
        if payload.get("error"):
            print(f"[eval] error: {payload['error']}")


async def run_eval_cli(options: EvalRunOptions, *, shutdown_tracing: bool = True) -> dict[str, Any]:
    service = EvalService()
    state = service.create_state(options)
    service.attach_emitter(state.run_id, print_event)
    result = await service.run(state, options)

    if shutdown_tracing and not options.skip_langfuse and options.benchmark != "smoke":
        from agents.observability import shutdown_tracing as _shutdown

        _shutdown()

    return result


def run_eval_cli_blocking(options: EvalRunOptions, *, shutdown_tracing: bool = True) -> dict[str, Any]:
    return asyncio.run(run_eval_cli(options, shutdown_tracing=shutdown_tracing))
