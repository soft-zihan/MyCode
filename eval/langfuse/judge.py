"""LLM-as-Judge — task_completion / trajectory 评估，score 写回 Langfuse。

使用项目同款 OpenAI 兼容 side model（环境变量 API/APIKEY 或 OPENAI_BASE_URL/OPENAI_API_KEY，
judge 模型可用 MYCODE_JUDGE_MODEL 覆盖）。输出 JSON {score: 0-1, reasoning}，
以 NUMERIC score + comment 提交。
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

JUDGE_MODEL_ENV = "MYCODE_JUDGE_MODEL"

TASK_COMPLETION_PROMPT = """\
You are an evaluation judge for an AI coding agent.

## User request
{user_input}

## Agent final output
{agent_output}

Judge whether the agent's final output plausibly completes the user's request.
You only see the request and the final output (not the full transcript).
Score 1.0 if the output directly addresses and plausibly completes the request,
0.5 if partially addressed or unclear, 0.0 if it fails, refuses, or is unrelated.

Respond with JSON only: {{"score": <float 0-1>, "reasoning": "<one sentence>"}}"""

TRAJECTORY_PROMPT = """\
You are an evaluation judge for an AI coding agent's execution trajectory.

## User request
{user_input}

## Tool call sequence
{tool_sequence}

Judge trajectory quality: Are the tool choices reasonable for the request?
Is there wasteful repetition, thrashing, or obvious wrong-tool usage?
Score 1.0 for an efficient sensible trajectory, 0.5 for noticeable waste,
0.0 for a clearly broken trajectory (endless loops, irrelevant tools only).

Respond with JSON only: {{"score": <float 0-1>, "reasoning": "<one sentence>"}}"""


def resolve_judge_config() -> tuple[str, str | None, str | None]:
    """返回 (model, api_base, api_key)。

    优先级：环境变量（MYCODE_JUDGE_MODEL / API / APIKEY / OPENAI_*）
    → load_config() 的第一个 endpoint（与后端服务同一数据源）。
    """
    model = os.environ.get(JUDGE_MODEL_ENV) or os.environ.get("MODEL")
    api_base = os.environ.get("API") or os.environ.get("OPENAI_BASE_URL") or None
    api_key = os.environ.get("APIKEY") or os.environ.get("OPENAI_API_KEY") or None

    if not (model and api_key):
        try:
            from agents.config import load_config
            config = load_config()
            if config.endpoints:
                ep = next(iter(config.endpoints.values()))
                model = model or ep.model
                api_key = api_key or ep.api_key
                api_base = api_base or ep.base_url
        except Exception:
            pass

    return model or "gpt-4o-mini", api_base, api_key


def _obs_attrs(obs: dict[str, Any]) -> dict[str, Any]:
    meta = obs.get("metadata") or {}
    if isinstance(meta, dict):
        attrs = meta.get("attributes")
        if isinstance(attrs, dict):
            return attrs
    return {}


def _tool_sequence_text(bundle: dict[str, Any], max_items: int = 30) -> str:
    """按时间排序的工具调用摘要（供 trajectory judge）。"""
    tools = sorted(
        (o for o in bundle.get("observations", []) if o.get("type") == "TOOL"),
        key=lambda o: o.get("startTime") or "",
    )
    lines = []
    for i, t in enumerate(tools[:max_items], 1):
        inp = t.get("input")
        if not isinstance(inp, str):
            inp = json.dumps(inp, ensure_ascii=False, default=str)
        status = "ERROR" if t.get("level") == "ERROR" else "ok"
        lines.append(f"{i}. {t.get('name')} [{status}] input={inp[:200]}")
    if len(tools) > max_items:
        lines.append(f"... ({len(tools) - max_items} more)")
    return "\n".join(lines) or "(no tool calls)"


def parse_judge_response(raw: str) -> dict[str, Any]:
    """解析 judge 输出中的 JSON（容忍 markdown 围栏/前后杂文）。"""
    m = re.search(r"\{[^{}]*\"score\"[^{}]*\}", raw, re.DOTALL)
    if not m:
        raise ValueError(f"judge response has no JSON score: {raw[:200]}")
    data = json.loads(m.group(0))
    score = float(data.get("score", 0.0))
    return {"score": max(0.0, min(1.0, score)), "reasoning": str(data.get("reasoning", ""))}


def _call_judge(prompt: str) -> dict[str, Any]:
    """调用 side model 完成一次 judge 评估。"""
    import openai

    model, api_base, api_key = resolve_judge_config()
    c = openai.OpenAI(api_key=api_key, base_url=api_base) if api_base else openai.OpenAI(api_key=api_key)
    resp = c.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        temperature=0,
    )
    return parse_judge_response(resp.choices[0].message.content or "")


def judge_task_completion(bundle: dict[str, Any]) -> dict[str, Any] | None:
    """task_completion：用户请求 vs 最终输出。缺 input/output 时跳过。"""
    user_input = bundle.get("input")
    agent_output = bundle.get("output")
    if not user_input or not agent_output:
        return None
    prompt = TASK_COMPLETION_PROMPT.format(
        user_input=str(user_input)[:3000],
        agent_output=str(agent_output)[:3000],
    )
    return _call_judge(prompt)


def judge_trajectory(bundle: dict[str, Any]) -> dict[str, Any] | None:
    """trajectory：用户请求 vs 工具调用序列。无工具调用时跳过。"""
    user_input = bundle.get("input")
    if not user_input:
        return None
    if not any(o.get("type") == "TOOL" for o in bundle.get("observations", [])):
        return None
    prompt = TRAJECTORY_PROMPT.format(
        user_input=str(user_input)[:2000],
        tool_sequence=_tool_sequence_text(bundle)[:6000],
    )
    return _call_judge(prompt)


def judge_trace(client: Any, trace_id: str, *, task_completion: bool = True, trajectory: bool = True) -> list[dict[str, Any]]:
    """对单条 trace 运行 LLM judges 并把分数写回 Langfuse。"""
    bundle = client.fetch_trace(trace_id)
    posted = []
    for enabled, fn, score_name in (
        (task_completion, judge_task_completion, "task_completion"),
        (trajectory, judge_trajectory, "trajectory_quality"),
    ):
        if not enabled:
            continue
        result = fn(bundle)
        if result is None:
            continue
        client.create_score(
            trace_id=trace_id,
            name=score_name,
            value=result["score"],
            data_type="NUMERIC",
            comment=f"[judge] {result['reasoning']}"[:1000],
        )
        posted.append({"name": score_name, **result})
    return posted
