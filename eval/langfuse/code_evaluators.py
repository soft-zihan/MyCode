"""Code evaluators — 确定性规则断言，score 写回 Langfuse。

评估对象：trace bundle = `LangfuseApiClient.fetch_trace(trace_id)` 返回的 JSON dict
（内嵌 observations，自定义可过滤字段位于 obs["metadata"]）。

指标（与 13-trace-eval-implementation-plan.md Phase 3 对应）：
- tool_success            NUMERIC  工具调用成功率（无 TOOL observation 时跳过）
- repeated_tool_calls     BOOLEAN  同一 (tool, input) 重复调用 >=3 次（True=检出重复，坏）
- event_range_complete    BOOLEAN  turn observation 携带完整 event_range（埋点质量自检）
"""

from __future__ import annotations

import json
from typing import Any, Callable

REPEAT_THRESHOLD = 3

ScoreResult = dict[str, Any]  # {"name", "value", "data_type", "comment"}


def _obs_metadata(obs: dict[str, Any]) -> dict[str, Any]:
    """提取 observation 的可过滤 metadata。"""
    meta = obs.get("metadata")
    return meta if isinstance(meta, dict) else {}


def _tools(bundle: dict[str, Any]) -> list[dict[str, Any]]:
    return [o for o in bundle.get("observations", []) if o.get("type") == "TOOL"]


def _agents(bundle: dict[str, Any]) -> list[dict[str, Any]]:
    return [o for o in bundle.get("observations", []) if o.get("type") == "AGENT"]


def eval_tool_success(bundle: dict[str, Any]) -> ScoreResult | None:
    """工具调用成功率：level != ERROR 的 TOOL observation 占比。"""
    tools = _tools(bundle)
    if not tools:
        return None
    ok = sum(1 for t in tools if t.get("level") != "ERROR")
    ratio = ok / len(tools)
    return {
        "name": "tool_success",
        "value": round(ratio, 3),
        "data_type": "NUMERIC",
        "comment": f"{ok}/{len(tools)} tool calls succeeded",
    }


def eval_repeated_tool_calls(bundle: dict[str, Any]) -> ScoreResult | None:
    """重复调用检测：同一 (name, input) 出现 >= REPEAT_THRESHOLD 次。"""
    tools = _tools(bundle)
    if not tools:
        return None
    seen: dict[tuple, int] = {}
    for t in tools:
        key_input = t.get("input")
        if not isinstance(key_input, str):
            key_input = json.dumps(key_input, ensure_ascii=False, sort_keys=True, default=str)
        key = (t.get("name"), key_input[:500])
        seen[key] = seen.get(key, 0) + 1
    repeated = {k: v for k, v in seen.items() if v >= REPEAT_THRESHOLD}
    if repeated:
        worst = max(repeated.items(), key=lambda kv: kv[1])
        return {
            "name": "repeated_tool_calls",
            "value": True,
            "data_type": "BOOLEAN",
            "comment": f"repeated: {worst[0][0]} x{worst[1]}",
        }
    return {
        "name": "repeated_tool_calls",
        "value": False,
        "data_type": "BOOLEAN",
        "comment": f"no tool called >= {REPEAT_THRESHOLD} times with identical input",
    }


def eval_subagent_timeout_loop(bundle: dict[str, Any]) -> ScoreResult | None:
    """子 Agent 超时/循环检测：区分长任务进展和重复失败。"""
    agents = _agents(bundle)
    if not agents:
        return None

    timeouts: list[dict[str, Any]] = []
    for obs in agents:
        meta = _obs_metadata(obs)
        status = str(meta.get("status") or "").lower()
        outcome = str(meta.get("outcome") or "").lower()
        output = str(obs.get("output") or "").lower()
        if outcome == "timeout" or status == "timeout" or "timed out" in output:
            timeouts.append(obs)

    if not timeouts:
        return {
            "name": "subagent_timeout_loop",
            "value": False,
            "data_type": "BOOLEAN",
            "comment": f"{len(agents)} sub-agent observation(s), no timeout",
        }

    repeated: dict[tuple, int] = {}
    progressing_timeouts = 0
    for obs in timeouts:
        meta = _obs_metadata(obs)
        key_input = obs.get("input")
        if not isinstance(key_input, str):
            key_input = json.dumps(key_input, ensure_ascii=False, sort_keys=True, default=str)
        key = (obs.get("name"), key_input[:500])
        repeated[key] = repeated.get(key, 0) + 1
        tool_calls = int(meta.get("tool_call_count") or 0)
        failed_calls = int(meta.get("failed_tool_call_count") or 0)
        if tool_calls >= 5 and failed_calls / max(tool_calls, 1) <= 0.5:
            progressing_timeouts += 1

    worst_count = max(repeated.values())
    looping = worst_count >= 2 or (len(timeouts) >= 3 and progressing_timeouts < len(timeouts))
    worst_name = max(repeated.items(), key=lambda kv: kv[1])[0][0] if repeated else "agent"
    return {
        "name": "subagent_timeout_loop",
        "value": looping,
        "data_type": "BOOLEAN",
        "comment": (
            f"timeouts={len(timeouts)}/{len(agents)}, repeated={worst_name} x{worst_count}, "
            f"progressing_timeouts={progressing_timeouts}"
        ),
    }


def eval_event_range_complete(bundle: dict[str, Any]) -> ScoreResult | None:
    """埋点质量自检：turn(chain) observation 是否携带完整 event_range。"""
    turns = [
        o for o in bundle.get("observations", [])
        if o.get("type") == "CHAIN"
        and o.get("name") == "turn"
        and _obs_metadata(o).get("turn_id")
    ]
    if not turns:
        return None
    complete = all(
        _obs_metadata(t).get("event_range_start_seq") is not None
        and _obs_metadata(t).get("event_range_end_seq") is not None
        for t in turns
    )
    return {
        "name": "event_range_complete",
        "value": complete,
        "data_type": "BOOLEAN",
        "comment": f"{len(turns)} turn observation(s) checked",
    }


ALL_EVALUATORS: list[Callable[[dict[str, Any]], ScoreResult | None]] = [
    eval_tool_success,
    eval_repeated_tool_calls,
    eval_subagent_timeout_loop,
    eval_event_range_complete,
]


def evaluate_bundle(bundle: dict[str, Any]) -> list[ScoreResult]:
    """对单个 trace bundle 运行全部 code evaluators（纯函数，不发网络请求）。"""
    results = []
    for fn in ALL_EVALUATORS:
        r = fn(bundle)
        if r is not None:
            results.append(r)
    return results


def evaluate_trace(client: Any, trace_id: str) -> list[ScoreResult]:
    """拉取 trace → 断言 → score 写回 Langfuse。"""
    bundle = client.fetch_trace(trace_id)
    results = evaluate_bundle(bundle)
    for r in results:
        client.create_score(
            trace_id=trace_id,
            name=r["name"],
            value=r["value"],
            data_type=r["data_type"],
            comment=r.get("comment"),
        )
    return results
