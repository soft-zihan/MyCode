"""MyCode agent 单任务执行器（运行于本项目 venv，由 loca_side 调起）。

读取 loca_task.json（user_prompt + agent_workspace，MCP 服务器经 workspace/.mcp.json
被 agent 原生加载），复用 run_agent_task（Langfuse trace + 超时 + 事件落盘 +
压缩触发计数），结果写 agent_result.json。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


async def _run(a: argparse.Namespace) -> int:
    task_dir = Path(a.task_dir).resolve()
    task = json.loads((task_dir / "loca_task.json").read_text(encoding="utf-8"))

    from eval.common.runner_base import init_eval_tracing, resolve_model_config, run_agent_task

    if not a.skip_langfuse:
        init_eval_tracing("loca")
    model, api_base, api_key = resolve_model_config(a.model, a.api_base)
    thinking = {"on": True, "off": False, "default": None}[a.thinking]

    result = await run_agent_task(
        task["user_prompt"],
        benchmark="loca",
        task_id=f"{task.get('name')}-{task.get('index')}",
        session_id=a.eval_session_id,
        model=model,
        api_base=api_base,
        api_key=api_key,
        timeout_s=a.timeout,
        thinking=thinking,
        compression_arm=a.arm,
        context_window=a.window,
        workspace=task["agent_workspace"],
    )
    result["model"] = model
    result["arm"] = a.arm
    result["window"] = a.window
    (task_dir / "agent_result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    print(
        f"[agent_main] done dur={result.get('duration_s')}s err={result.get('error')} "
        f"compression={result.get('compression_events')} tokens={result.get('tokens')}",
        flush=True,
    )
    return 0 if not result.get("error") else 1


def main() -> None:
    parser = argparse.ArgumentParser(description="LOCA 任务的 MyCode agent 执行器")
    parser.add_argument("--task-dir", required=True)
    parser.add_argument("--eval-session-id", required=True)
    parser.add_argument("--model", default=None)
    parser.add_argument("--api-base", default=None)
    parser.add_argument("--thinking", choices=["on", "off", "default"], default="off")
    parser.add_argument("--arm", choices=["full", "truncate", "tool_only", "session_only"], default="full")
    parser.add_argument("--window", type=int, default=None, help="context_window 覆盖（如 100000/1000000）")
    parser.add_argument("--timeout", type=int, default=0, help="单任务超时秒数（0=不限）")
    parser.add_argument("--skip-langfuse", action="store_true")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
