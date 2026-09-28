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

## User request (THIS turn only)
{user_input}

## Earlier turns in this session (context, oldest first)
{prior_context}

## Memory/wiki context injected before this turn
{retrieved_context}

## Tools called this turn (name [status] only)
{tools_summary}

## Agent final output
{agent_output}

Judge whether the agent's final output plausibly completes the user's request FOR THIS TURN.
You see the request, session context, injected memory context, tool names called this
turn, and the final output (not the full transcript).
IMPORTANT:
- Actions the agent took are evidenced by the tool list — if the requested action
  appears there (e.g. compact_context, agent.general dispatch), the agent DID perform
  it even when the prose output does not restate it.
- This turn may be one step of a multi-turn scenario: earlier turns define what is being
  asked (e.g. the user taught a workflow and THIS turn only asks to record/apply one step).
  Judge against THIS turn's request as informed by earlier turns — do not demand work
  that belongs to other turns.
- If injected memory context contains project rules (e.g. a user-taught
  convention), an answer following that rule is CORRECT even if it contradicts
  general-world knowledge — the rule was explicitly taught by the user.
- TEACHING scenario: when the user describes steps/workflows for the agent to RECORD
  (e.g. "我现在教你...", "请记住...", "record this workflow"), the correct behavior is to
  record and/or acknowledge — NOT to execute the described steps now. Never penalize
  for not executing taught steps.
Score 1.0 if the output directly addresses and plausibly completes the request,
0.5 if partially addressed or unclear, 0.0 if it fails, refuses, or is unrelated.

Respond with JSON only: {{"score": <float 0-1>, "reasoning": "<one sentence>"}}"""

TRAJECTORY_PROMPT = """\
You are an evaluation judge for an AI coding agent's execution trajectory.

## User request (THIS turn only)
{user_input}

## Earlier turns in this session (context, oldest first)
{prior_context}

## Memory/wiki context injected before this turn
{retrieved_context}

## Tool call sequence
{tool_sequence}

Judge trajectory quality FOR THIS TURN: Are the tool choices reasonable for the request?
Is there wasteful repetition, thrashing, or obvious wrong-tool usage?
Notes:
- Earlier turns define the multi-turn scenario; tools that belong to another step of the
  scenario are not this turn's responsibility.
- `agent.general` entries ARE sub-agent dispatches (the agent tool); their
  dispatch= JSON shows the actual kwargs (background/resume_session_id/...).
- A [CANCELLED] or [BLOCKED] status may reflect a USER-REQUESTED cancellation
  or permission denial — judge it against the request, not as an automatic failure.
- Answering from injected memory or earlier-turn context without tool calls can be
  the correct behavior.
- TEACHING scenario: when the user describes steps/workflows for the agent to RECORD
  (e.g. "我现在教你...", "第一步：运行 X", "record this workflow"), recording them (e.g. via
  remember) instead of executing them is the CORRECT trajectory — do not penalize
  "ignored the request to run X" when X was a taught step, not an execution request.
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
    # 后台派发的 AGENT span 生命周期跨轮：CANCELLED 几乎总是后续轮的用户取消/
    # 收尾清理。按轮评审时以"派发时刻"为准——当时已成功返回 state=running。
    if (
        obs.get("type") == "AGENT"
        and isinstance(metadata, dict)
        and metadata.get("background")
        and outcome == "cancelled"
    ):
        return "ok-at-dispatch; background job cancelled in a LATER turn (by subsequent user request or cleanup), not a dispatch failure"
    if obs.get("level") == "ERROR" or outcome in {"error", "timeout", "cancelled", "blocked"}:
        return (outcome or "error").upper()
    return "ok"


def _tool_line(index: int, obs: dict[str, Any]) -> str:
    inp = obs.get("input")
    if not isinstance(inp, str):
        inp = json.dumps(inp, ensure_ascii=False, default=str)
    # 截断必须显式标注——否则 judge 把显示截断误判为"写入内容不完整"
    shown = inp[:200] + (" …[display truncated; the actual call carried full input]" if len(inp) > 200 else "")
    line = f"{index}. {obs.get('name')} [{_tool_status(obs)}] input={shown}"
    # output 摘要：让 judge 看到调用实际效果（如 remember 的 action/path），
    # 避免"看不到结果 → 断言没做"的误读
    out = obs.get("output")
    if out is not None:
        if not isinstance(out, str):
            out = json.dumps(out, ensure_ascii=False, default=str)
        if out:
            line += " output=" + out[:150].replace("\n", " ")
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


def build_prior_context(
    client: Any,
    current: dict[str, Any],
    *,
    bundle_cache: dict[str, dict[str, Any]] | None = None,
    max_turns: int = 6,
    input_chars: int = 300,
    output_chars: int = 300,
) -> str:
    """M4-judge：同会话前序轮摘要（oldest first），消除逐轮评审的多阶段场景盲区。

    只用 trace 级 input/output（不拉 observations），按 timestamp 排序取最近
    max_turns 轮。无 sessionId（side-query 等）返回空串。
    """
    session_id = current.get("sessionId")
    if not session_id:
        return ""
    cache = bundle_cache if bundle_cache is not None else {}
    try:
        peers = client.fetch_traces(limit=50, session_id=session_id)
    except Exception:
        return ""
    ts = current.get("timestamp") or ""
    earlier = sorted(
        (t for t in peers if t.get("id") != current.get("id") and (t.get("timestamp") or "") < ts),
        key=lambda t: t.get("timestamp") or "",
    )[-max_turns:]
    lines: list[str] = []
    for t in earlier:
        bundle = cache.get(t["id"])
        if bundle is None:
            try:
                bundle = client.fetch_trace(t["id"])
            except Exception:
                continue
            cache[t["id"]] = bundle
        inp = str(bundle.get("input") or "").replace("\n", " ")[:input_chars]
        out = str(bundle.get("output") or "").replace("\n", " ")[:output_chars]
        lines.append(f"- user: {inp}\n  agent: {out}")
    return "\n".join(lines)


def _tools_summary_text(bundle: dict[str, Any]) -> str:
    """本轮工具调用一览（name [status]），供 task_completion judge——
    只看散文输出会把"做了但没复述"的行动判为未执行（compact/派发假 0 分类）。"""
    obs = sorted(_judgeable_tool_obs(bundle), key=lambda o: o.get("startTime") or "")
    if not obs:
        return "(none)"
    return ", ".join(f"{o.get('name')} [{_tool_status(o)}]" for o in obs)[:800]


def judge_task_completion(bundle: dict[str, Any], prior_context: str = "") -> dict[str, Any] | None:
    """task_completion：用户请求 vs 最终输出。缺 input/output 时跳过。"""
    user_input = bundle.get("input")
    agent_output = bundle.get("output")
    if not user_input or not agent_output:
        return None
    prompt = TASK_COMPLETION_PROMPT.format(
        user_input=_truncate_middle(user_input, head_chars=2000, tail_chars=1000),
        agent_output=_truncate_middle(agent_output, head_chars=1000, tail_chars=6000),
        retrieved_context=_retrieved_context_text(bundle) or "(none)",
        prior_context=prior_context or "(first turn of this session)",
        tools_summary=_tools_summary_text(bundle),
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


def judge_trajectory(bundle: dict[str, Any], prior_context: str = "") -> dict[str, Any] | None:
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
        prior_context=prior_context or "(first turn of this session)",
    )
    return _call_judge(prompt)


def judge_trace(client: Any, trace_id: str, *, task_completion: bool = True, trajectory: bool = True,
                prior_context: str = "") -> list[dict[str, Any]]:
    """对单条 trace 运行 LLM judges 并把分数写回 Langfuse。"""
    bundle = client.fetch_trace(trace_id)
    posted = []
    for enabled, fn, score_name in (
        (task_completion, judge_task_completion, "task_completion"),
        (trajectory, judge_trajectory, "trajectory_quality"),
    ):
        if not enabled:
            continue
        result = fn(bundle, prior_context)
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
