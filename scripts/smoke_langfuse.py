#!/usr/bin/env python3
"""Langfuse SDK smoke test."""

from __future__ import annotations

import os
import sys
import time
import uuid
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from agents.observability.langfuse_api import load_langfuse_env

load_langfuse_env(PROJECT_ROOT)
os.environ["MYCODE_TRACING"] = "1"

from agents.observability import flush_tracing, init_tracing, shutdown_tracing
from agents.observability.status import get_langfuse_status
from agents.observability.trace import trace_context, trace_span
from agents.observability.tracer import get_trace_client

init_tracing()
client = get_trace_client()
if client is None:
    print("[smoke] ❌ Langfuse client 初始化失败，请检查 MYCODE_TRACING 与 LANGFUSE_* 密钥")
    sys.exit(1)

smoke_id = f"smoke-{uuid.uuid4().hex[:8]}"
session_id = f"smoke-session-{uuid.uuid4().hex[:8]}"
trace_name = f"mycode-smoke-{smoke_id}"
usage_details = {
    "input": 80,
    "input_cached_tokens": 20,
    "output": 20,
    "total": 120,
}

print(f"[smoke] trace 标记: {smoke_id}, session: {session_id}")

with trace_context(
    session_id=session_id,
    trace_name=trace_name,
    tags=["smoke-test"],
    metadata={"smoke_id": smoke_id},
):
    with trace_span(
        "turn",
        input="smoke test: 帮我读取 config.py",
        metadata={
            "turn_id": smoke_id,
            "event_range_start_seq": 1,
        },
    ) as turn_span:
        with trace_span("model_call", model="test-model") as model_span:
            model_span.update(
                input={"messages": [{"role": "user", "content": "smoke"}]},
                output={"content": "ok"},
                usage_details=usage_details,
            )
            time.sleep(0.05)

        with trace_span(
            "tool_call",
            name="tool.read_file",
            input={"file_path": "config.py"},
            output="config content",
            metadata={"tool_name": "read_file"},
        ):
            time.sleep(0.05)

        with trace_span(
            "compact",
            metadata={
                "tokens_before": 5000,
                "tokens_after": 3000,
                "compression_ratio": 0.6,
            },
        ):
            time.sleep(0.02)

        turn_span.update(
            output="smoke completed",
            metadata={"event_range_end_seq": 42},
        )

flush_tracing()
print("[smoke] span 已 flush，等待 Langfuse 摄取...")

expected_types = {"CHAIN", "GENERATION", "TOOL"}
deadline = time.time() + 90
found = None
detail = None
observations: list = []
observation_types: set[str] = set()
generation = None
while time.time() < deadline:
    try:
        traces = client.api.trace.list(name=trace_name, limit=50).data
        found = next((trace for trace in traces if trace.name == trace_name), None)
        if found:
            detail = client.api.trace.get(found.id)
            observations = detail.observations or []
            observation_types = {str(observation.type).upper() for observation in observations}
            generation = next(
                (observation for observation in observations if str(observation.type).upper() == "GENERATION"),
                None,
            )
            if expected_types <= observation_types and generation:
                break
    except Exception as exc:
        print(f"[smoke] trace query failed: {type(exc).__name__}: {exc}")
    time.sleep(5)

if not found or detail is None:
    shutdown_tracing()
    print(f"[smoke] ❌ 90 秒内未查询到 trace（标记 {smoke_id}），请检查密钥/网络")
    sys.exit(1)

errors: list[str] = []
if detail.session_id != session_id:
    errors.append(f"sessionId={detail.session_id!r}, expected={session_id!r}")
if not expected_types <= observation_types:
    errors.append(
        f"missing observation types: present={sorted(observation_types)}, expected={sorted(expected_types)}"
    )
if not generation:
    errors.append("missing GENERATION observation")
else:
    generation_usage = generation.usage_details or {}
    if generation_usage.get("total") != usage_details["total"]:
        errors.append(f"usageDetails={generation_usage}, expected total={usage_details['total']}")
    if generation_usage.get("input") != usage_details["input"]:
        errors.append(f"usageDetails={generation_usage}, expected input={usage_details['input']}")
    if generation_usage.get("input_cached_tokens") != usage_details["input_cached_tokens"]:
        errors.append(
            f"usageDetails={generation_usage}, expected input_cached_tokens={usage_details['input_cached_tokens']}"
        )

status = get_langfuse_status(force=True)
project_id = status.get("project_id") or "*"
print(f"[smoke] ✅ trace 已到达 Langfuse: id={detail.id} name={detail.name}")
print(f"[smoke]    sessionId={detail.session_id} observations={len(observations)}")
print(f"[smoke]    UI: {status.get('endpoint', '')}/project/{project_id}/traces/{detail.id}")

shutdown_tracing()

if errors:
    for error in errors:
        print(f"[smoke] ❌ {error}")
    sys.exit(1)

print("[smoke] ✅ trace 结构、session 和 usage 校验通过")
