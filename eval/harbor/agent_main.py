"""容器内入口：用 MyCode 跑一道题，把结果写成 JSON 交给 harbor 侧读取。

由 `MyCodeAgent` 在题目容器里以 `python -m eval.harbor.agent_main` 起。
不走 MyCode 的 CLI —— 和 LOCA 一样进程内直接调 `run_agent_task`，这样两臂的
差异只在 agent 本身，不掺进 CLI 的参数解析和输出格式。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any

from eval.harbor.payload import (
    API_KEY_ENV,
    BASE_URL_ENV,
    settings_from_env,
    thinking_misalignment,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="eval.harbor.agent_main")
    parser.add_argument("--model", required=True)
    parser.add_argument("--workdir", required=True, help="题目工作区；aider-polyglot 是 /app")
    parser.add_argument("--instruction-file", required=True)
    parser.add_argument("--result", required=True, help="结果 JSON 落盘路径")
    parser.add_argument("--benchmark", default="aider_polyglot")
    parser.add_argument("--task-id", default="")
    parser.add_argument(
        "--timeout-s",
        type=int,
        default=0,
        help="0 = 不设内部超时，交给 harbor 的 agent 超时管，与 opencode 臂对齐",
    )
    return parser


def write_result(path: str | Path, payload: dict[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False, default=str), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    api_key = os.environ.get(API_KEY_ENV, "")
    api_base = os.environ.get(BASE_URL_ENV, "")
    missing = [name for name, value in ((API_KEY_ENV, api_key), (BASE_URL_ENV, api_base)) if not value]
    if missing:
        # 凭据只从环境变量取，不接受命令行传入：命令行会原样印进 trial.log
        print(f"missing env: {', '.join(missing)}", file=sys.stderr)
        return 2

    instruction = Path(args.instruction_file).read_text(encoding="utf-8")
    if not instruction.strip():
        print(f"empty instruction file: {args.instruction_file}", file=sys.stderr)
        return 2

    workdir = Path(args.workdir)
    if not workdir.is_dir():
        print(f"workdir is not a directory: {workdir}", file=sys.stderr)
        return 2

    # tracing 默认关闭（要 MYCODE_TRACING 才开），容器里没有 Langfuse 也不会卡住
    os.environ.setdefault("LANGFUSE_TRACING_ENABLED", "false")

    # 行为开关来自 harbor 的 agents[].env（runner 从宿主端点配置搬过来）；
    # 容器里没有 ~/.my-code/config.json，不搬就会退回内置默认值。
    settings = settings_from_env()

    from eval.common.runner_base import run_agent_task

    result = asyncio.run(
        run_agent_task(
            instruction,
            benchmark=args.benchmark,
            task_id=args.task_id or workdir.name,
            session_id=f"harbor-{uuid.uuid4().hex[:12]}",
            model=args.model,
            api_base=api_base,
            api_key=api_key,
            timeout_s=args.timeout_s,
            workspace=str(workdir),
            **settings,
        )
    )

    write_result(args.result, result)

    tokens = result.get("tokens") or {}
    print(
        "mycode result: error={error} duration_s={duration} in={inp} out={out} "
        "reasoning={reasoning} cached={cached} text_chars={chars} settings={settings}".format(
            error=result.get("error"),
            duration=result.get("duration_s"),
            inp=tokens.get("input"),
            out=tokens.get("output"),
            reasoning=tokens.get("reasoning"),
            cached=tokens.get("cached"),
            chars=len(result.get("text") or ""),
            settings=settings,
        )
    )
    # 变量对齐的兜底哨兵，判据在 payload.thinking_misalignment（纯函数，可单测）
    if warning := thinking_misalignment(result, settings):
        print(f"WARNING: {warning}", file=sys.stderr)
    # 只要结果落盘了就返回 0：MyCode 自己报错（超时、模型异常）算不算「这次 trial
    # 作废」由 harbor 侧的适配器判（payload.hard_failure），在这里退非零会让
    # exec_as_agent 直接抛异常，结果文件就再也读不到了。
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
