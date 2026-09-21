"""GAIA 评测 runner — 抽样 N 题跑 Pass@1，结果落盘 eval/reports/ 并关联 Langfuse trace。

数据：data/GAIA/all.json（165 题，字段 task_id/Question/Level/file_name/answer）
附件：data/GAIA/files/{file_name}

用法：
    .venv/bin/python -m eval.gaia.runner --sample 10 --seed 42
    .venv/bin/python -m eval.gaia.runner --sample 5 --level 1 --timeout 900
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
    extract_final_answer,
    gaia_question_scorer,
    init_eval_tracing,
    resolve_model_config,
    run_agent_task,
    write_reports,
)

DATA_PATH = PROJECT_ROOT / "data" / "GAIA" / "all.json"
FILES_DIR = PROJECT_ROOT / "data" / "GAIA" / "files"


def load_tasks(level: int | None = None) -> list[dict]:
    tasks = json.loads(DATA_PATH.read_text(encoding="utf-8"))
    if level:
        tasks = [t for t in tasks if t.get("Level") == level]
    return tasks


async def run(sample: int, seed: int, level: int | None, timeout: int, model: str | None, api_base: str | None) -> dict:
    from agents.observability import shutdown_tracing

    tasks = load_tasks(level)
    rng = random.Random(seed)
    selected = rng.sample(tasks, min(sample, len(tasks)))

    resolved_model, resolved_base, api_key = resolve_model_config(model, api_base)
    run_id = f"gaia-{time.strftime('%Y%m%d_%H%M%S')}-{uuid.uuid4().hex[:6]}"
    session_id = f"eval-{run_id}"
    init_eval_tracing("gaia")

    print(f"[gaia] run_id={run_id} model={resolved_model} tasks={len(selected)}")

    results = []
    for i, task in enumerate(selected, 1):
        task_id = task.get("task_id") or task.get("id")
        question = task["Question"]
        expected = str(task.get("answer", ""))
        file_name = task.get("file_name")
        attachment = None
        if file_name:
            candidate = FILES_DIR / file_name
            attachment = str(candidate) if candidate.exists() else None
            if attachment is None:
                print(f"  [{i}] ⚠️ 附件缺失: {file_name}")

        prompt = build_prompt(question, attachment=attachment)
        print(f"  [{i}/{len(selected)}] L{task.get('Level')} {task_id[:8]}… ", end="", flush=True)

        out = await run_agent_task(
            prompt,
            benchmark="gaia",
            task_id=task_id,
            session_id=session_id,
            model=resolved_model,
            api_base=resolved_base,
            api_key=api_key,
            timeout_s=timeout,
        )
        predicted = extract_final_answer(out["text"])
        correct = (not out["error"]) and gaia_question_scorer(expected, predicted)
        results.append({
            "task_id": task_id,
            "level": task.get("Level"),
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
    meta = {"model": resolved_model, "seed": seed, "level": level, "session_id": session_id, "timeout_s": timeout}
    json_path, md_path = write_reports("gaia", run_id, meta, summary, results)

    print(f"\n[gaia] Pass@1: {correct_n}/{total} = {summary['pass_at_1']:.1%}")
    print(f"[gaia] 报告: {json_path}")
    print(f"[gaia]       {md_path}")
    print(f"[gaia] Langfuse traces: session={session_id}")

    shutdown_tracing()
    return {"summary": summary, "json_path": str(json_path), "run_id": run_id}


def main() -> None:
    parser = argparse.ArgumentParser(description="GAIA 抽样评测 runner")
    parser.add_argument("--sample", type=int, default=10, help="抽样题数（默认 10）")
    parser.add_argument("--seed", type=int, default=42, help="抽样种子（默认 42）")
    parser.add_argument("--level", type=int, default=None, choices=[1, 2, 3], help="只跑指定 Level")
    parser.add_argument("--timeout", type=int, default=0, help="单题超时秒数（默认 0，不超时）")
    parser.add_argument("--model", type=str, default=None, help="覆盖模型")
    parser.add_argument("--api-base", type=str, default=None, help="覆盖 API base")
    args = parser.parse_args()
    asyncio.run(run(args.sample, args.seed, args.level, args.timeout, args.model, args.api_base))


if __name__ == "__main__":
    main()
