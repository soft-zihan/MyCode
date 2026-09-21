"""GAIA 评测 CLI — 通过共享 EvalService 抽样跑 Pass@1，结果落盘并关联 Langfuse trace / dataset。

数据：data/GAIA/all.json（165 题，字段 task_id/Question/Level/file_name/answer）
附件：data/GAIA/files/{file_name}

用法：
    .venv/bin/python -m eval.gaia.runner --sample 10 --seed 42
    .venv/bin/python -m eval.gaia.runner --sample 5 --level 1 --timeout 900
    .venv/bin/python -m eval.gaia.runner --sample 5 --execution-mode backend_session --judge
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from eval.common.cli import run_eval_cli_blocking  # noqa: E402
from eval.common.models import EvalRunOptions  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="GAIA 抽样评测 runner")
    parser.add_argument("--sample", type=int, default=10, help="抽样题数（默认 10）")
    parser.add_argument("--seed", type=int, default=42, help="抽样种子（默认 42）")
    parser.add_argument("--level", type=int, default=None, choices=[1, 2, 3], help="只跑指定 Level")
    parser.add_argument("--thinking", choices=["on", "off", "default"], default="default",
                        help="推理模型 thinking 开关（default=跟随全局配置）")
    parser.add_argument("--timeout", type=int, default=0, help="单题超时秒数（默认 0，不超时）")
    parser.add_argument("--model", type=str, default=None, help="覆盖模型")
    parser.add_argument("--api-base", type=str, default=None, help="覆盖 API base")
    parser.add_argument("--execution-mode", choices=["backend_session", "in_process"], default="in_process")
    parser.add_argument("--judge", action="store_true", help="结束后运行 code evaluator + LLM judge")
    parser.add_argument("--no-dataset", action="store_true", help="不同步 Langfuse Dataset")
    parser.add_argument("--skip-langfuse", action="store_true", help="跳过 Langfuse")
    args = parser.parse_args()

    options = EvalRunOptions(
        benchmark="gaia",
        sample=args.sample,
        seed=args.seed,
        level=args.level,
        thinking={"on": True, "off": False, "default": None}[args.thinking],
        timeout_s=args.timeout,
        model=args.model,
        api_base=args.api_base,
        execution_mode=args.execution_mode,
        sync_langfuse_dataset=not args.no_dataset,
        judge_after_run=args.judge,
        skip_langfuse=args.skip_langfuse,
    )
    result = run_eval_cli_blocking(options)
    if result.get("status") == "failed":
        raise SystemExit(result.get("error") or "GAIA eval failed")


if __name__ == "__main__":
    main()
