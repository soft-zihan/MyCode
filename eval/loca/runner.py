"""LOCA-bench 评测 CLI — MyCode 整只 agent 作为 scaffold，压缩消融 v2 主战场。

选型依据：LOCA-bench 可控上下文增长（任务语义固定、环境状态可调 8K-256K），
专为对比上下文管理策略设计；GAIA L3 任务上下文峰值仅 ~76K（利用率 7.8%），
压缩永不触发，不适合消融（BC-16）。

用法：
    .venv/bin/python -m eval.loca.runner --config-set 64k --window 100k --arm full
    .venv/bin/python -m eval.loca.runner --sample 10 --select head   # 固定前 10（确定性）
    .venv/bin/python -m eval.loca.runner --indices 0,7,14 --arm truncate --window 100k

采样：--select head（默认）取配置文件顺序前 N，跨 run 完全可复现；
--select random 用 --seed 随机抽 N；--indices 显式指定。
"""

from __future__ import annotations

import argparse
import json
import random
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from eval.common.runner_base import REPORTS_DIR, resolve_model_config, write_reports  # noqa: E402

EVAL_WORKSPACES = Path.home() / ".mycode" / "eval_workspaces"


def _sanitize(value: str) -> str:
    return "".join(c if c.isalnum() or c in "._-" else "_" for c in value)[:100] or "task"


def _parse_window(raw: str | None) -> int | None:
    if raw is None:
        return None
    s = str(raw).strip().lower().replace("_", "")
    if s.endswith("k"):
        return int(float(s[:-1]) * 1000)
    if s.endswith("m"):
        return int(float(s[:-1]) * 1_000_000)
    return int(s)


def _loca_commit(loca_repo: Path) -> str | None:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=loca_repo, capture_output=True, text=True, timeout=10
        ).stdout.strip() or None
    except Exception:
        return None


def select_indices(total: int, sample: int, select: str, seed: int, indices: str | None) -> list[int]:
    if indices:
        picked = sorted({int(x) for x in indices.split(",") if x.strip()})
        for i in picked:
            if not 0 <= i < total:
                raise SystemExit(f"--indices 越界: {i} (共 {total} 个配置)")
        return picked
    if select == "head":
        return list(range(min(sample, total)))
    rng = random.Random(seed)
    return sorted(rng.sample(range(total), min(sample, total)))


def main() -> None:
    parser = argparse.ArgumentParser(description="LOCA-bench 评测 runner（MyCode scaffold）")
    parser.add_argument("--config-set", default="64k",
                        choices=["8k", "16k", "32k", "64k", "96k", "128k", "256k"],
                        help="环境描述长度预设（默认 64k）")
    parser.add_argument("--sample", type=int, default=10, help="每批任务数（默认 10）")
    parser.add_argument("--select", choices=["head", "random"], default="head",
                        help="head=固定取前 N（确定性，默认）；random=按 --seed 随机抽样")
    parser.add_argument("--seed", type=int, default=42, help="random 抽样种子")
    parser.add_argument("--indices", default=None, help="显式配置下标，逗号分隔（覆盖 sample/select）")
    parser.add_argument("--arm", choices=["full", "truncate", "tool_only", "session_only"], default="full",
                        help="压缩消融臂（默认 full）")
    parser.add_argument("--window", default="100k",
                        help="context_window 覆盖：100k/1000k/整数（默认 100k 压力窗口）")
    parser.add_argument("--thinking", choices=["on", "off", "default"], default="off")
    parser.add_argument("--model", default=None, help="覆盖模型（默认取第一个端点）")
    parser.add_argument("--api-base", default=None)
    parser.add_argument("--timeout", type=int, default=1800, help="单任务 agent 超时秒数（默认 1800）")
    parser.add_argument("--max-tool-uses", type=int, default=100, help="LOCA env wrapper 工具上限")
    parser.add_argument("--loca-repo", default=str(PROJECT_ROOT / "projects" / "LOCA-bench"))
    parser.add_argument("--loca-venv-python", default=None,
                        help="LOCA venv 解释器（默认 {loca_repo}/.venv/bin/python）")
    parser.add_argument("--skip-langfuse", action="store_true")
    args = parser.parse_args()

    loca_repo = Path(args.loca_repo).resolve()
    loca_python = Path(args.loca_venv_python) if args.loca_venv_python else loca_repo / ".venv" / "bin" / "python"
    if not loca_python.exists():
        raise SystemExit(f"LOCA venv 解释器不存在: {loca_python}（先在 {loca_repo} 安装）")
    cfg_file = loca_repo / "task-configs" / f"final_{args.config_set}_set_config.json"
    if not cfg_file.exists():
        raise SystemExit(f"任务配置不存在: {cfg_file}")
    window = _parse_window(args.window)

    configurations = json.loads(cfg_file.read_text(encoding="utf-8"))["configurations"]
    picked = select_indices(len(configurations), args.sample, args.select, args.seed, args.indices)

    model, api_base, _ = resolve_model_config(args.model, args.api_base)
    run_id = f"loca-{time.strftime('%Y%m%d_%H%M%S')}"
    print(f"[loca] run_id={run_id} set={args.config_set} arm={args.arm} window={window} "
          f"model={model} tasks={len(picked)}/{len(configurations)} select={args.select}")

    rows: list[dict[str, Any]] = []
    for n, idx in enumerate(picked, 1):
        entry = configurations[idx]
        name = entry.get("name", f"config_{idx}")
        task_dir = EVAL_WORKSPACES / run_id / f"{idx:03d}-{_sanitize(name)}"
        task_dir.mkdir(parents=True, exist_ok=True)

        agent_cmd = [
            sys.executable, str(PROJECT_ROOT / "eval" / "loca" / "agent_main.py"),
            "--task-dir", str(task_dir),
            "--eval-session-id", run_id,
            "--thinking", args.thinking,
            "--arm", args.arm,
            "--timeout", str(args.timeout),
        ]
        if window is not None:
            agent_cmd += ["--window", str(window)]
        if args.model:
            agent_cmd += ["--model", args.model]
        if args.api_base:
            agent_cmd += ["--api-base", args.api_base]
        if args.skip_langfuse:
            agent_cmd.append("--skip-langfuse")

        side_cmd = [
            str(loca_python), str(PROJECT_ROOT / "eval" / "loca" / "loca_side.py"), "run-task",
            "--loca-repo", str(loca_repo),
            "--config-set", args.config_set,
            "--index", str(idx),
            "--task-dir", str(task_dir),
            "--agent-cmd", json.dumps(agent_cmd),
            "--timeout", str(args.timeout + 60),
            "--max-tool-uses", str(args.max_tool_uses),
        ]

        t0 = time.time()
        print(f"  [{n}/{len(picked)}] idx={idx} {name} …", flush=True)
        proc = subprocess.run(side_cmd, cwd=str(PROJECT_ROOT), capture_output=True, text=True,
                              timeout=args.timeout + 600)
        if proc.returncode != 0 and not (task_dir / "loca_eval.json").exists():
            print(f"    [SIDE ERROR] rc={proc.returncode} {(proc.stderr or '')[-500:]}", flush=True)

        ev = json.loads((task_dir / "loca_eval.json").read_text()) if (task_dir / "loca_eval.json").exists() else {}
        ar = json.loads((task_dir / "agent_result.json").read_text()) if (task_dir / "agent_result.json").exists() else {}
        reward = ev.get("reward")
        comp = ar.get("compression_events") or {}
        row = {
            "task_id": f"{args.config_set}-{idx}",
            "name": name,
            "index": idx,
            "reward": reward,
            "passed": isinstance(reward, (int, float)) and reward >= 0.999,
            "duration_s": round(time.time() - t0, 1),
            "tokens": ar.get("tokens") or {},
            "compression_events": comp,
            "trace_id": ar.get("trace_id"),
            "agent_error": ev.get("agent_error") or ar.get("error"),
            "eval_error": ev.get("eval_error"),
            "step_info": ev.get("step_info"),
            "task_dir": str(task_dir),
        }
        rows.append(row)
        mark = "✅" if row["passed"] else "❌"
        print(f"    {mark} reward={reward} dur={row['duration_s']}s comp={comp} err={row['agent_error'] or row['eval_error'] or ''}", flush=True)
        if proc.stdout:
            for line in proc.stdout.strip().splitlines()[-2:]:
                print(f"    | {line[:160]}", flush=True)

    n = len(rows)
    scored = [r for r in rows if isinstance(r["reward"], (int, float))]
    total_comp = {k: sum((r["compression_events"] or {}).get(k, 0) for r in rows)
                  for k in ("tool_folded", "session_folded", "events_hidden")}
    summary = {
        "total": n,
        "scored": len(scored),
        "passed": sum(1 for r in rows if r["passed"]),
        "pass_at_1": round(sum(1 for r in rows if r["passed"]) / n, 4) if n else 0.0,
        "avg_reward": round(sum(r["reward"] for r in scored) / len(scored), 4) if scored else None,
        "errors": sum(1 for r in rows if r["agent_error"] or r["eval_error"]),
        "compression_events_total": total_comp,
        "total_input_tokens": sum((r["tokens"] or {}).get("input", 0) for r in rows),
        "total_output_tokens": sum((r["tokens"] or {}).get("output", 0) for r in rows),
        "avg_duration_s": round(sum(r["duration_s"] for r in rows) / n, 1) if n else 0.0,
    }
    meta = {
        "benchmark_detail": "LOCA-bench (hkust-nlp)",
        "config_set": args.config_set,
        "select": args.select,
        "seed": args.seed,
        "indices": picked,
        "arm": args.arm,
        "window": window,
        "model": model,
        "thinking": args.thinking,
        "timeout_s": args.timeout,
        "max_tool_uses": args.max_tool_uses,
        "loca_repo": str(loca_repo),
        "loca_commit": _loca_commit(loca_repo),
        "scaffold": "mycode-agent",
    }

    # BC-16 触发验证：压缩臂在压力窗口下零触发 = 实验无效信号
    if args.arm != "truncate" and sum(total_comp.values()) == 0:
        print("\n⚠️  压缩事件计数为 0——本次运行未触发压缩，消融对比无效（BC-16）。"
              "检查窗口/环境描述长度组合是否足以越过阈值。", flush=True)

    json_path, md_path = write_reports("loca", run_id, meta, summary, rows,
                                       reports_dir=REPORTS_DIR, report_stem=run_id)
    print(f"[loca] completed {summary['passed']}/{n} pass@1={summary['pass_at_1']:.1%} "
          f"avg_reward={summary['avg_reward']} comp={total_comp}")
    print(f"[loca] report: {json_path}\n[loca]         {md_path}")


if __name__ == "__main__":
    main()
