"""HLE 评测 CLI — 通过共享 EvalService 抽样跑 Pass@1，结果落盘并关联 Langfuse trace / dataset。

数据：data/HLE/all_500.json（500 题，字段 id/question/answer/answer_type/image/category）
含 image 的题目默认跳过；--include-image 可强制包含（仅提供路径）。

用法：
    .venv/bin/python -m eval.hle.runner --sample 10 --seed 42
    .venv/bin/python -m eval.hle.runner --sample 5 --category "Physics" --timeout 900
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
    parser = argparse.ArgumentParser(description="HLE 抽样评测 runner")
    parser.add_argument("--sample", type=int, default=10, help="抽样题数（默认 10）")
    parser.add_argument("--seed", type=int, default=42, help="抽样种子（默认 42）")
    parser.add_argument("--category", type=str, default=None, help="只跑指定 category")
    parser.add_argument("--include-image", action="store_true", help="包含带图片的题（默认跳过）")
    parser.add_argument("--timeout", type=int, default=0, help="单题超时秒数（默认 0，不超时）")
    parser.add_argument("--model", type=str, default=None, help="覆盖模型")
    parser.add_argument("--api-base", type=str, default=None, help="覆盖 API base")
    parser.add_argument("--execution-mode", choices=["backend_session", "in_process"], default="in_process")
    parser.add_argument("--judge", action="store_true", help="结束后运行 code evaluator + LLM judge")
    parser.add_argument("--no-dataset", action="store_true", help="不同步 Langfuse Dataset")
    parser.add_argument("--skip-langfuse", action="store_true", help="跳过 Langfuse")
    args = parser.parse_args()

    options = EvalRunOptions(
        benchmark="hle",
        sample=args.sample,
        seed=args.seed,
        category=args.category,
        include_image=args.include_image,
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
        raise SystemExit(result.get("error") or "HLE eval failed")


if __name__ == "__main__":
    main()
