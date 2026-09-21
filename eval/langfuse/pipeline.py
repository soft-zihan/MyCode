from __future__ import annotations

from typing import Any, Callable

from eval.langfuse.code_evaluators import evaluate_bundle
from eval.langfuse.judge import judge_trace


def evaluate_traces(
    client: Any,
    trace_ids: list[str],
    *,
    judge: bool = False,
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    total_scores = 0

    for trace_id in trace_ids:
        bundle = client.fetch_trace(trace_id)
        scores = evaluate_bundle(bundle)
        for score in scores:
            client.create_score(
                trace_id=trace_id,
                name=score["name"],
                value=score["value"],
                data_type=score["data_type"],
                comment=score.get("comment"),
            )
        total_scores += len(scores)
        entry: dict[str, Any] = {
            "trace_id": trace_id,
            "code_scores": scores,
            "judge_scores": [],
        }

        if judge:
            try:
                posted = judge_trace(client, trace_id)
                entry["judge_scores"] = posted
            except Exception as e:
                entry["judge_error"] = f"{type(e).__name__}: {e}"

        results.append(entry)
        if progress:
            progress({"type": "eval/trace_evaluated", "trace_id": trace_id, "result": entry})

    return {
        "traces": len(results),
        "code_scores": total_scores,
        "judge": judge,
        "results": results,
    }
