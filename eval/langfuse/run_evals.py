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


def main() -> None:
    parser = argparse.ArgumentParser(description="MyCode Langfuse 评估管道")
    parser.add_argument("--limit", type=int, default=20, help="扫描 trace 数（默认 20）")
    parser.add_argument("--from-hours", type=int, default=24, help="只看最近 N 小时（默认 24）")
    parser.add_argument("--trace-id", type=str, default=None, help="只评估指定 trace")
    parser.add_argument("--judge", action="store_true", help="运行 LLM-as-Judge（task_completion/trajectory）")
    parser.add_argument("--sync-failures", action="store_true", help="失败案例回流 regression dataset")
    parser.add_argument("--name", type=str, default=None, help="按 trace name 过滤（如 agent-turn）")
    parser.add_argument("--diagnose", action="store_true", help="轨迹诊断（Failure Onset 定位 + 失败分类）")
    parser.add_argument("--export-training", type=str, default=None, help="导出高质量轨迹到文件（JSONL 格式）")
    args = parser.parse_args()

    from agents.observability.langfuse_api import LangfuseApiClient, load_langfuse_env
    from eval.langfuse.code_evaluators import evaluate_bundle

    load_langfuse_env(PROJECT_ROOT)
    client = LangfuseApiClient()

    from_ts = (datetime.now(timezone.utc) - timedelta(hours=args.from_hours)).isoformat()

    if args.trace_id:
        trace_ids = [args.trace_id]
    else:
        traces = client.fetch_traces(limit=args.limit, from_timestamp=from_ts, name=args.name)
        trace_ids = [t["id"] for t in traces if t.get("id")]
    print(f"[eval] 待评估 traces: {len(trace_ids)}")

    total_scores = 0
    for tid in trace_ids:
        bundle = client.fetch_trace(tid)
        scores = evaluate_bundle(bundle)
        for s in scores:
            client.create_score(
                trace_id=tid,
                name=s["name"],
                value=s["value"],
                data_type=s["data_type"],
                comment=s.get("comment"),
            )
        total_scores += len(scores)
        line = " ".join(f"{s['name']}={s['value']}" for s in scores) or "(no scores)"
        print(f"  {tid[:16]}  {line}")

        if args.judge:
            from eval.langfuse.judge import judge_trace
            try:
                posted = judge_trace(client, tid)
                for p in posted:
                    print(f"    [judge] {p['name']}={p['score']:.2f}  {p['reasoning'][:80]}")
            except Exception as e:
                print(f"    [judge] failed: {e}")

    print(f"[eval] 完成：{len(trace_ids)} traces / {total_scores} code scores")

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
