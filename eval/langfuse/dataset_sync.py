"""失败案例回流 — 从 Langfuse trace 中筛出失败案例，写入 regression dataset。

失败判定（任一满足）：
- 存在 level == ERROR 的 observation
- code evaluator 分数不达标（tool_success < 0.8 或 repeated_tool_calls == True）

dataset item 采用确定性 id（trace-{trace_id}），upsert 幂等。
"""

from __future__ import annotations

import time
from typing import Any

DATASET_NAME = "regression-failures"
TOOL_SUCCESS_THRESHOLD = 0.8


def _obs_has_error(bundle: dict[str, Any]) -> list[str]:
    return [
        f"{o.get('name')} ({o.get('type')})"
        for o in bundle.get("observations", [])
        if o.get("level") == "ERROR"
    ]


def _score_failures(scores: list[dict[str, Any]]) -> list[str]:
    reasons = []
    for s in scores:
        if s["name"] == "tool_success" and s["value"] < TOOL_SUCCESS_THRESHOLD:
            reasons.append(f"tool_success={s['value']}")
        if s["name"] == "repeated_tool_calls" and s["value"] is True:
            reasons.append("repeated_tool_calls")
    return reasons


def classify_failure(bundle: dict[str, Any], scores: list[dict[str, Any]]) -> list[str]:
    """返回失败原因列表（空 = 非失败案例）。"""
    reasons = [f"error_obs: {n}" for n in _obs_has_error(bundle)]
    reasons += _score_failures(scores)
    return reasons


def export_failures_to_dataset(
    client: Any,
    dataset_name: str = DATASET_NAME,
    limit: int = 50,
    from_timestamp: str | None = None,
) -> dict[str, Any]:
    """扫描最近 traces，把失败案例 upsert 进 dataset。

    Returns: {"scanned": N, "exported": N, "dataset": name, "items": [...]}
    """
    client.create_dataset(
        dataset_name,
        description="MyCode 失败案例回流（自动导出，供回归实验复跑）",
    )
    traces = client.fetch_traces(limit=limit, from_timestamp=from_timestamp)
    exported = []
    for t in traces:
        trace_id = t.get("id")
        if not trace_id:
            continue
        bundle = client.fetch_trace(trace_id)
        from eval.langfuse.code_evaluators import evaluate_bundle
        scores = evaluate_bundle(bundle)
        reasons = classify_failure(bundle, scores)
        if not reasons:
            continue
        client.upsert_dataset_item(
            dataset_name=dataset_name,
            input={"user_message": bundle.get("input") or t.get("input")},
            metadata={
                "trace_id": trace_id,
                "session_id": bundle.get("sessionId") or t.get("sessionId"),
                "failure_reasons": reasons,
                "exported_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            },
            item_id=f"trace-{trace_id}",
        )
        exported.append({"trace_id": trace_id, "reasons": reasons})
    return {
        "scanned": len(traces),
        "exported": len(exported),
        "dataset": dataset_name,
        "items": exported,
    }
