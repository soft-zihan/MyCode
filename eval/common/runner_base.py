"""评测 runner 共享基础设施 — Agent 驱动、trace_id 捕获、答案抽取、评分、报告落盘。

设计：
- 每个任务包一层 `eval_task` root span（benchmark/task_id 元数据），
  agent 的 turn span 成为其子 span；OTel trace_id 即 Langfuse trace id，
  直接从 span context 捕获写入报告，实现"评测结果 ↔ Langfuse trace"关联。
- 逐任务 force_flush，跑完即可在 Langfuse 回查。
- 报告统一写 eval/reports/{benchmark}_{ts}.json + .md。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
import unicodedata
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
REPORTS_DIR = PROJECT_ROOT / "eval" / "reports"

FINAL_ANSWER_RE = re.compile(r"FINAL ANSWER\s*[:：]\s*(.+)", re.IGNORECASE | re.DOTALL)


def resolve_model_config(cli_model: str | None = None, cli_api_base: str | None = None) -> tuple[str, str, str]:
    """解析 (model, api_base, api_key)。

    模型名命中端点配置时 base/key 成对取自该端点（禁止与环境变量跨源混配），
    CLI --api-base 可显式覆盖；未命中端点时用环境变量对
    （API/OPENAI_BASE_URL/OPENAI_API_BASE + APIKEY/OPENAI_API_KEY）；
    两者皆无则取第一个端点。api_base 无法解析时直接报错——
    禁止静默回落 api.openai.com（OpenAI SDK 对 base_url=None 的默认行为）。
    """
    from agents.config import load_config

    model = cli_model or os.environ.get("MODEL")
    env_base = os.environ.get("API") or os.environ.get("OPENAI_BASE_URL") or os.environ.get("OPENAI_API_BASE")
    env_key = os.environ.get("APIKEY") or os.environ.get("OPENAI_API_KEY")

    config = load_config()
    endpoint = next((e for e in config.endpoints.values() if e.model == model), None) if model else None
    if endpoint is None and not model and config.endpoints:
        endpoint = next(iter(config.endpoints.values()))
        model = endpoint.model

    if endpoint is not None:
        api_base = cli_api_base or endpoint.base_url
        api_key = endpoint.api_key or env_key
    else:
        api_base = cli_api_base or env_base
        api_key = env_key

    if not model:
        raise RuntimeError("无可用模型：CLI/--model、环境变量 MODEL 未指定且未配置任何端点")
    if not api_key:
        raise RuntimeError("无可用 API key（检查 ~/.my-code/config.json 端点配置或 APIKEY/OPENAI_API_KEY 环境变量）")
    if not api_base:
        raise RuntimeError(
            f"无法解析 api_base：模型 {model!r} 未命中任何端点配置，且环境未提供 "
            "API/OPENAI_BASE_URL/OPENAI_API_BASE；拒绝静默回落 api.openai.com"
        )
    return model, api_base, api_key


def init_eval_tracing(benchmark: str) -> None:
    """初始化 OTel → Langfuse（评测场景强制开启，密钥缺失时静默降级）。"""
    from agents.observability.langfuse_api import load_langfuse_env
    load_langfuse_env(PROJECT_ROOT)
    os.environ["MYCODE_TRACING"] = "1"
    from agents.observability import init_tracing
    init_tracing()


def extract_final_answer(text: str) -> str:
    """抽取最终答案：优先 'FINAL ANSWER:' 标记，否则取末尾非空行。"""
    m = FINAL_ANSWER_RE.search(text or "")
    if m:
        return m.group(1).strip().splitlines()[0].strip()
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    return lines[-1] if lines else ""


def normalize_answer(s: str) -> str:
    """标准化答案：NFKC、去标点、去冠词、小写、压缩空白。"""
    s = unicodedata.normalize("NFKC", str(s))
    s = s.lower()
    s = re.sub(r"[^\w\s.\-]", " ", s)
    s = re.sub(r"\b(a|an|the)\b", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def exact_match(expected: str, predicted: str) -> bool:
    return normalize_answer(expected) == normalize_answer(predicted)


def gaia_question_scorer(expected: str, predicted: str) -> bool:
    """GAIA 官方风格评分：数字按 float 比较；逗号列表按集合比较；否则标准化字符串相等。"""
    expected, predicted = str(expected).strip(), str(predicted).strip()

    def to_float(x: str) -> float | None:
        try:
            return float(x.replace(",", "").replace("$", "").replace("%", ""))
        except ValueError:
            return None

    fe, fp = to_float(expected), to_float(predicted)
    if fe is not None and fp is not None:
        return abs(fe - fp) < 1e-6

    if "," in expected:
        set_e = {normalize_answer(p) for p in expected.split(",") if p.strip()}
        set_p = {normalize_answer(p) for p in predicted.split(",") if p.strip()}
        if set_e and set_e == set_p:
            return True

    return normalize_answer(expected) == normalize_answer(predicted)


async def run_agent_task(
    prompt: str,
    *,
    benchmark: str,
    task_id: str,
    session_id: str,
    model: str,
    api_base: str | None,
    api_key: str,
    timeout_s: int = 0,
    thinking: bool | None = None,
    compression_arm: str | None = None,
    workspace: str,
) -> dict[str, Any]:
    """运行单个评测任务，返回 {text, duration_s, tokens, trace_id, error, agent_session_id}。

    workspace 必填：任务产物必须隔离在专属工作区，禁止落到仓库根目录。
    """
    from agents.agent import Agent
    from agents.observability import flush_tracing
    from agents.observability.trace import trace_context, trace_span

    t0 = time.time()
    result: dict[str, Any] = {"text": "", "duration_s": 0.0, "tokens": {}, "trace_id": None, "error": None, "agent_session_id": None}

    with trace_context(
        session_id=session_id,
        trace_name=f"{benchmark}-eval",
        tags=["eval", benchmark],
        metadata={
            "benchmark": benchmark,
            "task_id": task_id,
            "eval_session_id": session_id,
            "model": model,
        },
    ):
        with trace_span(
            "eval_task",
            name=f"eval.{benchmark}",
            input=prompt[:4000],
            metadata={
                "benchmark": benchmark,
                "task_id": task_id,
            },
        ) as span:
            result["trace_id"] = span.get_trace_id()
            agent = None
            try:
                agent = Agent(
                    model=model,
                    api_base=api_base,
                    api_key=api_key,
                    permission_mode="bypassPermissions",
                    is_sub_agent=True,
                    thinking=thinking,
                    compression_arm=compression_arm,
                    workspace=workspace,
                )
                result["agent_session_id"] = agent.session_id
                agent_task = asyncio.create_task(agent.run_once(prompt))
                if timeout_s > 0:
                    out = await asyncio.wait_for(agent_task, timeout=timeout_s)
                else:
                    out = await agent_task
                result["text"] = out.get("text", "")
                result["tokens"] = out.get("tokens", {})
            except asyncio.TimeoutError:
                result["error"] = f"timeout after {timeout_s}s"
                span.add_metadata(timeout_s=timeout_s)
                span.record_error(TimeoutError(result["error"]))
            except Exception as e:
                result["error"] = f"{type(e).__name__}: {e}"
                span.record_error(e)
            finally:
                span.update(
                    output=result["text"][:20000] if result["text"] else (result["error"] or ""),
                    metadata={
                        "duration_s": round(time.time() - t0, 2),
                        "success": result["error"] is None,
                    },
                )

            if agent and agent.session:
                try:
                    from agents.core.session import session_dir
                    import json
                    events_path = session_dir() / f"{agent.session_id}.events.jsonl"
                    with events_path.open("w", encoding="utf-8") as f:
                        for event in agent.session.events:
                            f.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
                    result["events_file"] = str(events_path)
                except Exception as e:
                    result["events_error"] = str(e)

        result["duration_s"] = round(time.time() - t0, 2)
        flush_tracing()
    return result


def build_prompt(question: str, *, attachment: str | None = None, answer_hint: str = "") -> str:
    parts = [question.strip()]
    if attachment:
        parts.append(f"\nAttachment file (read it with your tools if needed): {attachment}")
    parts.append(
        "\nWork through the problem with your tools. "
        "When done, end your reply with exactly one line:\nFINAL ANSWER: <answer>\n"
        "(give only the final value — no units, no extra words, unless the question asks for them)"
    )
    if answer_hint:
        parts.append(f"Answer format hint: {answer_hint}")
    return "\n".join(parts)


def write_reports(
    benchmark: str,
    run_id: str,
    meta: dict[str, Any],
    summary: dict[str, Any],
    results: list[dict[str, Any]],
    reports_dir: Path | None = None,
    report_stem: str | None = None,
) -> tuple[Path, Path]:
    """写 JSON + Markdown 报告，返回两个路径。"""
    out_dir = reports_dir or REPORTS_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = report_stem or f"{benchmark}_{time.strftime('%Y%m%d_%H%M%S')}"
    json_path = out_dir / f"{stem}.json"
    md_path = out_dir / f"{stem}.md"

    report = {
        "benchmark": benchmark,
        "run_id": run_id,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "meta": meta,
        "summary": summary,
        "results": results,
    }
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        f"# {benchmark.upper()} 评测报告",
        "",
        f"- run_id: `{run_id}`",
        f"- model: `{meta.get('model')}`",
        f"- 样本: {summary.get('total')} 题 (seed={meta.get('seed')})",
        f"- **Pass@1: {summary.get('correct', 0)}/{summary.get('total', 0)} = {summary.get('pass_at_1', 0):.1%}**",
        f"- 平均耗时: {summary.get('avg_duration_s', 0):.1f}s",
        f"- Langfuse session: `{meta.get('session_id')}`",
        "",
        "| # | task_id | 正确 | 耗时(s) | trace_id | 备注 |",
        "|---|---------|------|---------|----------|------|",
    ]
    for i, r in enumerate(results, 1):
        mark = "✅" if r.get("correct") else "❌"
        note = r.get("error") or ""
        if not note:
            note = f"expected={str(r.get('expected'))[:30]} predicted={str(r.get('predicted'))[:30]}"
        lines.append(
            f"| {i} | `{str(r.get('task_id'))[:12]}` | {mark} | {r.get('duration_s', 0)} | `{str(r.get('trace_id'))[:12]}` | {note[:60]} |"
        )
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return json_path, md_path
