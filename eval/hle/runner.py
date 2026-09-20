"""HLE (Humanity's Last Exam) 评测 runner — 抽样 N 题跑 Pass@1，结果落盘并关联 Langfuse trace。

数据：data/HLE/all_500.json（500 题，字段 id/question/answer/answer_type/image/category）
含 image 的题目默认跳过（当前 Agent 无视觉输入）；--include-image 可强制包含（仅提供路径）。

用法：
    .venv/bin/python -m eval.hle.runner --sample 10 --seed 42
    .venv/bin/python -m eval.hle.runner --sample 5 --category "Physics" --timeout 900
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys
import time
import uuid
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from eval.common.runner_base import (  # noqa: E402
    build_prompt,
    exact_match,
    extract_final_answer,
    init_eval_tracing,
    resolve_model_config,
    run_agent_task,
    write_reports,
)

DATA_PATH = PROJECT_ROOT / "data" / "HLE" / "all_500.json"
IMAGES_DIR = PROJECT_ROOT / "data" / "HLE" / "images"


def load_tasks(category: str | None = None, include_image: bool = False) -> list[dict]:
    tasks = json.loads(DATA_PATH.read_text(encoding="utf-8"))
    if not include_image:
        tasks = [t for t in tasks if not t.get("image")]
    if category:
        tasks = [t for t in tasks if t.get("category") == category]
    return tasks


async def run(sample: int, seed: int, category: str | None, include_image: bool, timeout: int, model: str | None, api_base: str | None) -> dict:
    from agents.observability import shutdown_tracing

    tasks = load_tasks(category, include_image)
    rng = random.Random(seed)
    selected = rng.sample(tasks, min(sample, len(tasks)))

    resolved_model, resolved_base, api_key = resolve_model_config(model, api_base)
    run_id = f"hle-{time.strftime('%Y%m%d_%H%M%S')}-{uuid.uuid4().hex[:6]}"
    session_id = f"eval-{run_id}"
    init_eval_tracing("hle")

    print(f"[hle] run_id={run_id} model={resolved_model} tasks={len(selected)}")

    results = []
    for i, task in enumerate(selected, 1):
        task_id = task.get("id")
        question = task["question"]
        expected = str(task.get("answer", ""))
        answer_type = task.get("answer_type", "")

        attachment = None
        if task.get("image"):
            candidate = IMAGES_DIR / str(task["image"])
            attachment = str(candidate) if candidate.exists() else None

        hint = "exact match (multiple choice letter)" if answer_type == "multiple-choice" else "exact match"
        prompt = build_prompt(question, attachment=attachment, answer_hint=hint)
        print(f"  [{i}/{len(selected)}] {str(task_id)[:10]}… ", end="", flush=True)

        out = await run_agent_task(
            prompt,
            benchmark="hle",
            task_id=str(task_id),
            session_id=session_id,
            model=resolved_model,
            api_base=resolved_base,
            api_key=api_key,
            timeout_s=timeout,
        )
        predicted = extract_final_answer(out["text"])
        correct = (not out["error"]) and exact_match(expected, predicted)
        results.append({
            "task_id": task_id,
            "category": task.get("category"),
            "answer_type": answer_type,
            "question_preview": question[:150],
            "expected": expected,
            "predicted": predicted,
            "correct": correct,
            "trace_id": out["trace_id"],
            "duration_s": out["duration_s"],
            "tokens": out["tokens"],
            "error": out["error"],
        })
        mark = "✅" if correct else "❌"
        print(f"{mark} {out['duration_s']}s  expected={expected[:30]!r} predicted={predicted[:30]!r}")

    total = len(results)
    correct_n = sum(1 for r in results if r["correct"])
    summary = {
        "total": total,
        "correct": correct_n,
        "pass_at_1": round(correct_n / total, 4) if total else 0.0,
        "avg_duration_s": round(sum(r["duration_s"] for r in results) / total, 1) if total else 0.0,
        "errors": sum(1 for r in results if r["error"]),
    }
    meta = {"model": resolved_model, "seed": seed, "category": category, "include_image": include_image, "session_id": session_id, "timeout_s": timeout}
    json_path, md_path = write_reports("hle", run_id, meta, summary, results)

    print(f"\n[hle] Pass@1: {correct_n}/{total} = {summary['pass_at_1']:.1%}")
    print(f"[hle] 报告: {json_path}")
    print(f"[hle]       {md_path}")
    print(f"[hle] Langfuse traces: session={session_id}")

    shutdown_tracing()
    return {"summary": summary, "json_path": str(json_path), "run_id": run_id}


def main() -> None:
    parser = argparse.ArgumentParser(description="HLE 抽样评测 runner")
    parser.add_argument("--sample", type=int, default=10, help="抽样题数（默认 10）")
    parser.add_argument("--seed", type=int, default=42, help="抽样种子（默认 42）")
    parser.add_argument("--category", type=str, default=None, help="只跑指定 category")
    parser.add_argument("--include-image", action="store_true", help="包含带图片的题（默认跳过）")
    parser.add_argument("--timeout", type=int, default=600, help="单题超时秒数（默认 600）")
    parser.add_argument("--model", type=str, default=None, help="覆盖模型")
    parser.add_argument("--api-base", type=str, default=None, help="覆盖 API base")
    args = parser.parse_args()
    asyncio.run(run(args.sample, args.seed, args.category, args.include_image, args.timeout, args.model, args.api_base))


if __name__ == "__main__":
    main()
