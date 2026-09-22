from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path
from typing import Any

from eval.common.models import EvalRunOptions, EvalRunState, EvalTask
from eval.common.runner_base import run_agent_task

# 评测工作区必须在仓库之外：评测任务模拟"独立用户项目"，而规则加载会从
# workspace 向上继承（正确的生产语义），嵌在仓库内会误继承开发 AGENTS.md（BC-17）
WORKSPACES_DIR = Path.home() / ".mycode" / "eval_workspaces"


def sanitize_id(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value)[:120] or "task"


def task_workspace(run_id: str, task_id: str) -> Path:
    path = WORKSPACES_DIR / sanitize_id(run_id) / sanitize_id(task_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


async def execute_task(
    task: EvalTask,
    state: EvalRunState,
    options: EvalRunOptions,
    model: str,
    api_base: str | None,
    api_key: str | None,
) -> dict[str, Any]:
    if options.execution_mode == "backend_session":
        return await execute_backend_session_task(task, state, options, model, api_base, api_key)
    return await execute_in_process_task(task, state, options, model, api_base, api_key)


async def execute_in_process_task(
    task: EvalTask,
    state: EvalRunState,
    options: EvalRunOptions,
    model: str,
    api_base: str | None,
    api_key: str | None,
) -> dict[str, Any]:
    out = await run_agent_task(
        task.prompt,
        benchmark=task.benchmark,
        task_id=task.task_id,
        session_id=state.eval_session_id,
        model=model,
        api_base=api_base,
        api_key=api_key or "",
        timeout_s=options.timeout_s,
        thinking=options.thinking,
        compression_arm=options.compression_arm,
        workspace=str(task_workspace(state.run_id, task.task_id)),
    )
    out["execution_mode"] = "in_process"
    return out


async def execute_backend_session_task(
    task: EvalTask,
    state: EvalRunState,
    options: EvalRunOptions,
    model: str,
    api_base: str | None,
    api_key: str | None,
) -> dict[str, Any]:
    from agents.observability import flush_tracing
    from agents.observability.trace import trace_context, trace_span
    from agents.session_manager import get_session_manager

    sm = get_session_manager()
    workspace = task_workspace(state.run_id, task.task_id)
    agent, session = sm.create(
        model=model,
        permission_mode="bypassPermissions",
        cwd=str(workspace),
        api_base=api_base,
        api_key=api_key,
        thinking=options.thinking,
        compression_arm=options.compression_arm,
        title=f"eval:{task.benchmark}:{task.task_id}",
        metadata={
            "eval_run_id": state.run_id,
            "benchmark": task.benchmark,
            "task_id": task.task_id,
            "eval_session_id": state.eval_session_id,
        },
    )

    started = time.time()
    result: dict[str, Any] = {
        "text": "",
        "duration_s": 0.0,
        "tokens": {},
        "trace_id": None,
        "error": None,
        "agent_session_id": session.id,
        "execution_mode": "backend_session",
        "workspace": str(workspace),
    }

    with trace_context(
        session_id=state.eval_session_id,
        trace_name=f"{task.benchmark}-eval",
        tags=["eval", task.benchmark, "backend_session"],
        metadata={
            "benchmark": task.benchmark,
            "task_id": task.task_id,
            "eval_session_id": state.eval_session_id,
            "eval_run_id": state.run_id,
            "model": model,
            "agent_session_id": session.id,
        },
    ):
        with trace_span(
            "eval_task",
            name=f"eval.{task.benchmark}",
            input=task.prompt[:4000],
            metadata={
                "benchmark": task.benchmark,
                "task_id": task.task_id,
                "agent_session_id": session.id,
                "workspace": str(workspace),
                "timeout_s": options.timeout_s,
            },
        ) as span:
            result["trace_id"] = span.get_trace_id()
            session.append("eval/task_metadata", {
                "run_id": state.run_id,
                "benchmark": task.benchmark,
                "task_id": task.task_id,
                "trace_id": result["trace_id"],
                "expected_output": task.expected_output,
                "level": task.level,
                "category": task.category,
                "metadata": task.metadata,
            })
            agent_task = asyncio.create_task(agent.run_once(task.prompt))
            sm.register_chat_task(session.id, agent_task)
            try:
                if options.timeout_s > 0:
                    out = await asyncio.wait_for(agent_task, timeout=options.timeout_s)
                else:
                    out = await agent_task
                result["text"] = out.get("text", "")
                result["tokens"] = out.get("tokens", {})
            except asyncio.TimeoutError:
                agent_task.cancel()
                result["error"] = f"timeout after {options.timeout_s}s"
                span.add_metadata(timeout_s=options.timeout_s)
                span.record_error(TimeoutError(result["error"]))
            except asyncio.CancelledError:
                result["error"] = "aborted"
                span.add_metadata(aborted=True)
                raise
            except Exception as e:
                result["error"] = f"{type(e).__name__}: {e}"
                span.record_error(e)
            finally:
                result["duration_s"] = round(time.time() - started, 2)
                try:
                    await agent.save()
                except Exception as e:
                    result["save_error"] = f"{type(e).__name__}: {e}"
                span.update(
                    output=result["text"][:20000] if result["text"] else (result["error"] or ""),
                    metadata={
                        "duration_s": result["duration_s"],
                        "success": result["error"] is None,
                        "agent_session_id": session.id,
                    },
                )
                flush_tracing()

    return result
