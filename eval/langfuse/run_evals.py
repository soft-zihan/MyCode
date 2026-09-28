"""评估管道 CLI — code evaluators + LLM judge + 失败回流 + 轨迹诊断。

用法：
    .venv/bin/python -m eval.langfuse.run_evals                     # 最近 24h 的 traces 跑 code evaluators
    .venv/bin/python -m eval.langfuse.run_evals --judge             # 追加 LLM-as-Judge
    .venv/bin/python -m eval.langfuse.run_evals --sync-failures     # 失败案例回流 dataset
    .venv/bin/python -m eval.langfuse.run_evals --trace-id <id>     # 只评估单条 trace
    .venv/bin/python -m eval.langfuse.run_evals --limit 50 --from-hours 48
    .venv/bin/python -m eval.langfuse.run_evals --diagnose          # 轨迹诊断（Failure Onset 定位）
    .venv/bin/python -m eval.langfuse.run_evals --diagnose --export-training  # 导出高质量轨迹用于训练
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# BC-24：评审对象=评审标准对齐——默认只评主任务 trace，排除派生 trace。
# session-title（标题 side query）与子代理 turn（tag=sub-agent）不是被评任务
# 的执行主体，按父任务标准评审会产出系统性假 0 分（详见 21-bad-case 台账 BC-24）。
DERIVED_TRACE_NAMES = frozenset({"session-title"})
DERIVED_TRACE_TAGS = ("sub-agent", "side-query")


def select_main_traces(traces: list[dict]) -> tuple[list[str], dict[str, int]]:
    """筛出主任务 trace id，并返回排除计数（可观测，不静默）。

    排除依据（真实 Langfuse 数据核验）：派生 trace 的 name 可能仍是
    agent-turn（side query 在子代理/标题上下文内自开 trace 时继承外层 tags），
    因此 name 与 tags 双通道过滤：session-title 按 name，sub-agent/side-query 按 tag。
    """
    ids: list[str] = []
    excluded = {name: 0 for name in DERIVED_TRACE_NAMES}
    excluded.update({tag: 0 for tag in DERIVED_TRACE_TAGS})
    for t in traces:
        name = t.get("name") or ""
        tags = t.get("tags") or []
        if name in DERIVED_TRACE_NAMES:
            excluded[name] += 1
            continue
        if derived_tag := next((tag for tag in DERIVED_TRACE_TAGS if tag in tags), None):
            excluded[derived_tag] += 1
            continue
        if tid := t.get("id"):
            ids.append(tid)
    return ids, {k: v for k, v in excluded.items() if v}


def main() -> None:
    parser = argparse.ArgumentParser(description="MyCode Langfuse 评估管道")
    parser.add_argument("--limit", type=int, default=20, help="扫描 trace 数（默认 20）")
    parser.add_argument("--from-hours", type=float, default=24, help="只看最近 N 小时（默认 24，支持小数如 0.5）")
    parser.add_argument("--from-timestamp", type=str, default=None,
                        help="ISO 时间下界（优先于 --from-hours，精确窗口如单 run 收口）")
    parser.add_argument("--to-timestamp", type=str, default=None, help="ISO 时间上界")
    parser.add_argument("--trace-id", type=str, default=None, help="只评估指定 trace")
    parser.add_argument("--judge", action="store_true", help="运行 LLM-as-Judge（task_completion/trajectory）")
    parser.add_argument("--sync-failures", action="store_true", help="失败案例回流 regression dataset")
    parser.add_argument("--name", type=str, default=None, help="按 trace name 过滤（如 agent-turn）")
    parser.add_argument("--include-derived", action="store_true",
                        help="评审含派生 trace（session-title/子代理）；默认排除（BC-24）")
    parser.add_argument("--diagnose", action="store_true", help="轨迹诊断（Failure Onset 定位 + 失败分类）")
    parser.add_argument("--export-training", type=str, default=None, help="导出高质量轨迹到文件（JSONL 格式）")
    args = parser.parse_args()

    from agents.observability.langfuse_api import LangfuseApiClient, load_langfuse_env
    from eval.langfuse.pipeline import evaluate_traces

    load_langfuse_env(PROJECT_ROOT)
    client = LangfuseApiClient()

    from_ts = args.from_timestamp or (datetime.now(timezone.utc) - timedelta(hours=args.from_hours)).isoformat()

    if args.trace_id:
        trace_ids = [args.trace_id]
        traces = []
    else:
        traces = client.fetch_traces(limit=args.limit, from_timestamp=from_ts,
                                     to_timestamp=args.to_timestamp, name=args.name)
        if args.include_derived:
            trace_ids = [t["id"] for t in traces if t.get("id")]
        else:
            trace_ids, excluded = select_main_traces(traces)
            if excluded:
                print(f"[eval] BC-24 筛选：排除派生 trace {excluded}（--include-derived 可关闭）")
    print(f"[eval] 待评估 traces: {len(trace_ids)}")

    # M4-judge：逐轮评审注入同会话前序轮上下文（多阶段教学/执行轮的假阴性根治）
    prior_contexts: dict[str, str] = {}
    if args.judge:
        from eval.langfuse.judge import build_prior_context
        meta_by_id = {t["id"]: t for t in traces}
        bundle_cache: dict[str, dict] = {}
        for tid in trace_ids:
            current = meta_by_id.get(tid) or client.fetch_trace(tid)
            ctx = build_prior_context(client, current, bundle_cache=bundle_cache)
            if ctx:
                prior_contexts[tid] = ctx
        print(f"[eval] 会话上下文：{len(prior_contexts)}/{len(trace_ids)} traces 带前序轮")

    def progress(payload: dict) -> None:
        result = payload["result"]
        line = " ".join(f"{s['name']}={s['value']}" for s in result["code_scores"]) or "(no scores)"
        print(f"  {result['trace_id'][:16]}  {line}")
        for judge_score in result.get("judge_scores", []):
            print(f"    [judge] {judge_score['name']}={judge_score['score']:.2f}  {judge_score['reasoning'][:80]}")
        if judge_error := result.get("judge_error"):
            print(f"    [judge] failed: {judge_error}")

    summary = evaluate_traces(client, trace_ids, judge=args.judge,
                              prior_contexts=prior_contexts, progress=progress)
    print(f"[eval] 完成：{summary['traces']} traces / {summary['code_scores']} code scores")

    if args.sync_failures:
        from eval.langfuse.dataset_sync import export_failures_to_dataset
        result = export_failures_to_dataset(client, limit=args.limit, from_timestamp=from_ts)
        print(f"[eval] 失败回流：scanned={result['scanned']} exported={result['exported']} → dataset '{result['dataset']}'")
        for item in result["items"]:
            print(f"  {item['trace_id'][:16]}  {item['reasons']}")

    if args.diagnose:
        from eval.langfuse.trajectory_diagnoser import (
            TrajectoryDiagnoser,
            compute_trajectory_metrics,
        )
        from eval.langfuse.failure_classifier import (
            FailureClassifier,
            compute_failure_distribution,
        )
        from eval.langfuse.trajectory_filter import (
            TrajectoryFilter,
            compute_filter_metrics,
            export_for_training,
        )

        diagnoser = TrajectoryDiagnoser()
        classifier = FailureClassifier()
        filter = TrajectoryFilter()

        print(f"\n[diagnose] 轨迹诊断开始...")
        
        all_diagnoses = []
        all_classifications = []
        all_filtered = []
        
        for tid in trace_ids:
            bundle = client.fetch_trace(tid)
            
            diagnosis = diagnoser.diagnose(bundle, tid)
            all_diagnoses.append(diagnosis)
            
            classification = classifier.classify(bundle, tid)
            all_classifications.append(classification)
            
            filtered = filter.filter(diagnosis)
            all_filtered.append(filtered)
            
            onset_str = f"Onset@{diagnosis.failure_onset.step_index}" if diagnosis.failure_onset else "No Onset"
            mode_str = classification.primary_mode.value if classification.is_failure else "success"
            print(f"  {tid[:16]}  {onset_str:<12}  {mode_str:<15}  HQ={len(diagnosis.high_quality_steps)}/{diagnosis.total_steps}")
        
        metrics = compute_trajectory_metrics(all_diagnoses)
        failure_dist = compute_failure_distribution(all_classifications)
        filter_metrics = compute_filter_metrics(all_filtered)
        
        print(f"\n[diagnose] 汇总指标:")
        print(f"  总 traces: {metrics['total_traces']}")
        print(f"  有 Failure Onset: {metrics['traces_with_failure_onset']} ({metrics['onset_rate']:.0%})")
        print(f"  平均质量比例: {metrics['avg_quality_ratio']:.1%}")
        print(f"  总步骤: {metrics['total_steps']} → 高质量: {metrics['total_high_quality_steps']}")
        
        if failure_dist:
            print(f"\n[diagnose] 失败模式分布:")
            for mode, count in sorted(failure_dist.items(), key=lambda x: -x[1]):
                print(f"  {mode}: {count}")
        
        if args.export_training:
            exported = export_for_training(all_filtered, args.export_training)
            print(f"\n[diagnose] 导出高质量轨迹: {exported} 条 → {args.export_training}")
            print(f"  训练可用步骤: {filter_metrics['training_ready_steps']}")


if __name__ == "__main__":
    main()
