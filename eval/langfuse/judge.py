"""LLM-as-Judge — task_completion / trajectory 评估，score 写回 Langfuse。

使用项目同款 OpenAI 兼容 side model（环境变量 API/APIKEY 或 OPENAI_BASE_URL/OPENAI_API_KEY，
judge 模型可用 MYCODE_JUDGE_MODEL 覆盖）。输出 JSON {score: 0-1, reasoning}，
以 NUMERIC score + comment 提交。
"""

from __future__ import annotations

import json
import os
import re
from collections import Counter
from typing import Any

JUDGE_MODEL_ENV = "MYCODE_JUDGE_MODEL"

TASK_COMPLETION_PROMPT = """\
You are an evaluation judge for an AI coding agent.

## User request
{user_input}

## Memory/wiki context injected before this turn
{retrieved_context}

## Agent final output
{agent_output}

Judge whether the agent's final output plausibly completes the user's request.
You only see the request, injected memory context, and the final output (not the full transcript).
IMPORTANT: If injected memory context contains project rules (e.g. a user-taught
convention), an answer following that rule is CORRECT even if it contradicts
general-world knowledge — the rule was explicitly taught by the user.
Score 1.0 if the output directly addresses and plausibly completes the request,
0.5 if partially addressed or unclear, 0.0 if it fails, refuses, or is unrelated.

Respond with JSON only: {{"score": <float 0-1>, "reasoning": "<one sentence>"}}"""

TRAJECTORY_PROMPT = """\
You are an evaluation judge for an AI coding agent's execution trajectory.

## User request
{user_input}

## Memory/wiki context injected before this turn
{retrieved_context}

## Tool call sequence
{tool_sequence}

Judge trajectory quality: Are the tool choices reasonable for the request?
Is there wasteful repetition, thrashing, or obvious wrong-tool usage?
Notes:
- `agent.general` entries ARE sub-agent dispatches (the agent tool); their
  dispatch= JSON shows the actual kwargs (background/resume_session_id/...).
- A [CANCELLED] or [BLOCKED] status may reflect a USER-REQUESTED cancellation
  or permission denial — judge it against the request, not as an automatic failure.
- Answering from injected memory without tool calls can be the correct behavior.
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


def _truncate_middle(text: Any, *, head_chars: int = 1000, tail_chars: int = 4000) -> str:
    value = str(text or "")
    if len(value) <= head_chars + tail_chars:
        return value
    return f"{value[:head_chars]}\n...[truncated {len(value) - head_chars - tail_chars} chars]...\n{value[-tail_chars:]}"


def _tool_status(obs: dict[str, Any]) -> str:
    metadata = obs.get("metadata") or {}
    outcome = str(metadata.get("outcome") or "").lower()
    if obs.get("level") == "ERROR" or outcome in {"error", "timeout", "cancelled", "blocked"}:
        return (outcome or "error").upper()
    return "ok"


def _tool_line(index: int, obs: dict[str, Any]) -> str:
    inp = obs.get("input")
    if not isinstance(inp, str):
        inp = json.dumps(inp, ensure_ascii=False, default=str)
    line = f"{index}. {obs.get('name')} [{_tool_status(obs)}] input={inp[:200]}"
    # BC-35：AGENT observation = agent 工具派发，附上 kwargs（background/
    # resume_session_id/agent_type），否则 judge 看不到参数误判"未按要求派发"
    if obs.get("type") == "AGENT":
        meta = obs.get("metadata") or {}
        kwargs = {
            k: meta[k]
            for k in ("agent_type", "background", "resume", "resume_session_id")
            if isinstance(meta, dict) and meta.get(k) is not None
        }
        if kwargs:
            line += " dispatch=" + json.dumps(kwargs, ensure_ascii=False, default=str)
    return line


# BC-35：系统级 setup span（非模型选择的工具），混入序列会误导 judge
# （"only initialized MCP without any subsequent tool calls"类假 0 分）
_SYSTEM_SPAN_NAMES = frozenset({"mcp.init"})


def _judgeable_tool_obs(bundle: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        o for o in bundle.get("observations", [])
        if o.get("type") in ("TOOL", "AGENT") and o.get("name") not in _SYSTEM_SPAN_NAMES
    ]


def _tool_sequence_text(
    bundle: dict[str, Any],
    *,
    head_items: int = 15,
    tail_items: int = 15,
    failure_items: int = 20,
) -> str:
    """按时间排序的工具调用摘要（供 trajectory judge）。

    长轨迹不只看前 30 个工具，而是保留 head、tail、失败/超时/blocked 样本和工具计数。
    """
    # BC-34：子代理派发（agent 工具）落盘为 AGENT observation（agent.general），
    # 只筛 TOOL 会系统性漏掉派发调用 → judge 误判"未派发子代理"给假 0 分
    tools = sorted(_judgeable_tool_obs(bundle), key=lambda o: o.get("startTime") or "")
    if not tools:
        return "(no tool calls)"

    indexed = list(enumerate(tools, 1))
    selected: dict[int, tuple[int, dict[str, Any]]] = {}
    for item in indexed[:head_items] + indexed[-tail_items:]:
        selected[item[0]] = item

    failures = [(i, t) for i, t in indexed if _tool_status(t) != "ok"]
    for item in failures[:failure_items]:
        selected[item[0]] = item

    lines = [
        f"total_tool_calls={len(tools)}",
        "tool_counts=" + json.dumps(dict(Counter(t.get("name") for _, t in indexed)), ensure_ascii=False),
        f"failed_or_blocked_tool_calls={len(failures)}",
    ]
    for index, tool in sorted(selected.values()):
        lines.append(_tool_line(index, tool))
    omitted = len(tools) - len(selected)
    if omitted > 0:
        lines.append(f"... ({omitted} selected/summarized tool calls omitted)")
    return "\n".join(lines)


def _extract_score_object(raw: str) -> str | None:
    """BC-35：从 judge 输出提取含 "score" 的 JSON 对象。

    旧实现用 `\\{[^{}]*"score"[^{}]*\\}` 正则——reasoning 内出现嵌套花括号
    （如 {'status': 'ok'}）即匹配失败，把合法响应误报为 no JSON score。
    改为字符串感知的花括号配平扫描。
    """
    start = raw.find("{")
    while start != -1:
        depth = 0
        in_string = False
        escaped = False
        for i in range(start, len(raw)):
            ch = raw[i]
            if in_string:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_string = False
                continue
            if ch == '"':
                in_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    candidate = raw[start:i + 1]
                    if '"score"' in candidate:
                        return candidate
                    break
        start = raw.find("{", start + 1)
    return None


def parse_judge_response(raw: str) -> dict[str, Any]:
    """解析 judge 输出中的 JSON（容忍 markdown 围栏/前后杂文/嵌套花括号）。"""
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.DOTALL).strip()
    data: Any = None
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        fragment = _extract_score_object(raw)
        if fragment is not None:
            data = json.loads(fragment)
    if not isinstance(data, dict) or "score" not in data:
        raise ValueError(f"judge response has no JSON score: {raw[:200]}")
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
        user_input=_truncate_middle(user_input, head_chars=2000, tail_chars=1000),
        agent_output=_truncate_middle(agent_output, head_chars=1000, tail_chars=6000),
        retrieved_context=_retrieved_context_text(bundle) or "(none)",
    )
    return _call_judge(prompt)


def _retrieved_context_text(bundle: dict[str, Any]) -> str:
    """BC-35：wiki 召回注入的上下文（RETRIEVER observation 的 output/metadata）。

    judge 只见 trace input/output——不知道记忆注入内容时，会把"遵循 wiki
    规则的回答"（如教学规则 pnpm）按通用常识误判为错误。
    """
    parts: list[str] = []
    for o in bundle.get("observations", []):
        if o.get("type") != "RETRIEVER" or not str(o.get("name") or "").startswith("wiki.recall"):
            continue
        out = o.get("output")
        if isinstance(out, str) and out.strip():
            parts.append(out.strip()[:1500])
        else:
            meta = o.get("metadata") or {}
            entries = meta.get("entries") if isinstance(meta, dict) else None
            if entries:
                parts.append("recalled entries: " + ", ".join(str(e) for e in entries))
    return "\n---\n".join(parts)[:4000]


def judge_trajectory(bundle: dict[str, Any]) -> dict[str, Any] | None:
    """trajectory：用户请求 vs 工具调用序列。无可评审工具调用时跳过。"""
    user_input = bundle.get("input")
    if not user_input:
        return None
    if not _judgeable_tool_obs(bundle):
        return None
    retrieved = _retrieved_context_text(bundle)
    prompt = TRAJECTORY_PROMPT.format(
        user_input=_truncate_middle(user_input, head_chars=1500, tail_chars=500),
        tool_sequence=_tool_sequence_text(bundle)[:12000],
        retrieved_context=retrieved or "(none)",
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
