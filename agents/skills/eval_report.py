from __future__ import annotations

import json
from typing import Any

from agents._utils import (
    utc_now as _utc_now,
    read_json as _read_json,
    read_jsonl as _read_jsonl,
    write_json as _write_json,
    ratio as _ratio,
    pct as _pct,
)
from .skill_file_ops import (
    ONLINE_PROVENANCE_INDEX,
    ONLINE_PROVENANCE_LOG,
    get_evolution_dir,
    load_skill_stats,
)
from .eval_rules import (
    SideQuery,
    _compile_eval_rules,
    _skill_status,
)
from .eval_replay import (
    DEFAULT_MIN_REPLAY_SAMPLES,
    DEFAULT_MIN_PROMOTION_TESTS,
    DEFAULT_MIN_RULE_PASS_RATE,
    _active_skill_snapshots,
    _rows_by_skill,
    _build_replay_pool,
    _summarize_rule_outcomes_async,
    _build_candidate_eval_bundle_async,
)
from .eval_champion import (
    ONLINE_EVAL_DIR,
    _lineage_id_for_skill,
    _persist_eval_artifacts,
)


def _action_bucket(action: str) -> str:
    raw = str(action or "none").strip().lower() or "none"
    if raw.endswith("_denied"):
        return "denied"
    if raw in {"add", "merge", "discard", "none", "failed"}:
        return raw
    return "other"


async def _evaluate_online_skill_evolution_core(
    *,
    min_replay_samples: int = DEFAULT_MIN_REPLAY_SAMPLES,
    min_promotion_tests: int = DEFAULT_MIN_PROMOTION_TESTS,
    min_rule_pass_rate: float = DEFAULT_MIN_RULE_PASS_RATE,
    write_report: bool = True,
    write_artifacts: bool = True,
    side_query: SideQuery | None = None,
    include_llm_rules: bool = False,
) -> dict[str, Any]:
    root = get_evolution_dir()
    provenance_rows = _read_jsonl(root / ONLINE_PROVENANCE_LOG)
    provenance_index = _read_json(root / ONLINE_PROVENANCE_INDEX, {})
    lifecycle_stats = load_skill_stats()
    active_skills = _active_skill_snapshots()
    grouped_rows = _rows_by_skill(provenance_rows)

    action_counts = {"none": 0, "add": 0, "merge": 0, "discard": 0, "failed": 0, "denied": 0, "other": 0}
    ok_count = 0
    candidate_events = 0
    accepted_events = 0
    recent_failures: list[dict[str, Any]] = []
    for row in provenance_rows:
        action = str(row.get("action") or "none")
        bucket = _action_bucket(action)
        action_counts[bucket] = int(action_counts.get(bucket, 0)) + 1
        if row.get("ok"):
            ok_count += 1
        if bucket not in {"none", "failed", "denied"}:
            candidate_events += 1
        if bucket in {"add", "merge"} and row.get("ok"):
            accepted_events += 1
        if bucket in {"failed", "denied"} or row.get("error"):
            recent_failures.append(
                {
                    "time": row.get("time", ""),
                    "action": action,
                    "skill": row.get("skill", ""),
                    "error": row.get("error", ""),
                }
            )

    all_names = set(active_skills)
    if isinstance(provenance_index, dict):
        all_names.update(str(name) for name in provenance_index if str(name).strip())
    all_names.update(str(name) for name in lifecycle_stats if str(name).strip())

    skills: list[dict[str, Any]] = []
    for name in sorted(all_names):
        lineage_raw = provenance_index.get(name, {}) if isinstance(provenance_index, dict) else {}
        lifecycle_raw = lifecycle_stats.get(name, {}) if isinstance(lifecycle_stats, dict) else {}
        lineage = lineage_raw if isinstance(lineage_raw, dict) else {}
        lifecycle = lifecycle_raw if isinstance(lifecycle_raw, dict) else {}
        snapshot = active_skills.get(name, {})
        if not snapshot:
            snapshot = {
                "name": name,
                "description": str(lineage.get("description") or lifecycle.get("description") or ""),
                "when_to_use": str(lineage.get("when_to_use") or ""),
                "instructions": "",
            }

        replay_pool = _build_replay_pool(name, grouped_rows.get(name, []), lineage, freeze=write_artifacts)
        rules = _compile_eval_rules(snapshot, include_llm_rules=include_llm_rules)
        rule_summary = await _summarize_rule_outcomes_async(
            rules,
            replay_pool,
            skill_name=name,
            side_query=side_query,
        )
        public_rule_summary = dict(rule_summary)
        public_rule_summary.pop("outcomes", None)
        promotion_test_count = sum(1 for item in replay_pool if item.get("split") == "promotion_test")
        status, reasons = _skill_status(
            replay_count=len(replay_pool),
            promotion_test_count=promotion_test_count,
            rule_summary=rule_summary,
            min_replay_samples=min_replay_samples,
            min_promotion_tests=min_promotion_tests,
            min_rule_pass_rate=min_rule_pass_rate,
        )
        current_version = (
            lineage.get("current_version", lifecycle.get("version", ""))
        )
        snapshot["version"] = str(current_version or "")
        candidate_bundle = await _build_candidate_eval_bundle_async(
            lineage_id=_lineage_id_for_skill(name),
            snapshot=snapshot,
            replay_pool=replay_pool,
            rules=rules,
            rule_summary=rule_summary,
            side_query=side_query,
        )
        artifacts = (
            _persist_eval_artifacts(
                skill_name=name,
                snapshot=snapshot,
                replay_pool=replay_pool,
                rules=rules,
                rule_summary=rule_summary,
                status=status,
                reasons=reasons,
                candidate_bundle=candidate_bundle,
            )
            if write_artifacts
            else {}
        )
        skills.append(
            {
                "skill": name,
                "lineage_id": _lineage_id_for_skill(name),
                "status": status,
                "reasons": reasons,
                "source_count": int(lineage.get("source_count", 0) or 0) if isinstance(lineage, dict) else 0,
                "history_count": int(lineage.get("history_count", 0) or 0) if isinstance(lineage, dict) else 0,
                "last_action": lineage.get("last_action", "") if isinstance(lineage, dict) else "",
                "last_time": lineage.get("last_time", "") if isinstance(lineage, dict) else "",
                "current_version": current_version,
                "created": int(lifecycle.get("created", 0) or 0),
                "evolutions": int(lifecycle.get("evolutions", 0) or 0),
                "invocations": int(lifecycle.get("invocations", 0) or 0),
                "feedback": int(lifecycle.get("feedback", 0) or 0),
                "replay": {
                    "count": len(replay_pool),
                    "mutate_dev": sum(1 for item in replay_pool if item.get("split") == "mutate_dev"),
                    "promotion_test": promotion_test_count,
                    "sources": sorted({str(item.get("source_type") or "") for item in replay_pool if item.get("source_type")}),
                },
                "eval": public_rule_summary,
                "candidate_eval": {
                    "candidate_count": len(list(candidate_bundle.get("candidate_variants") or [])) if isinstance(candidate_bundle, dict) else 0,
                    "best_candidate": dict(candidate_bundle.get("best_dev_summary") or {}) if isinstance(candidate_bundle, dict) else {},
                    "has_promotion_test_eval": bool((candidate_bundle or {}).get("best_test_summary")) if isinstance(candidate_bundle, dict) else False,
                },
                "artifacts": artifacts,
                "file": lifecycle.get("file", ""),
                "skill_dir": snapshot.get("skill_dir", ""),
            }
        )

    status_counts: dict[str, int] = {}
    champion_status_counts: dict[str, int] = {}
    total_replay = 0
    total_rule_outcomes = 0
    total_rule_passed = 0
    total_llm_rules = 0
    total_llm_rule_outcomes = 0
    total_llm_rule_passed = 0
    total_candidate_variants = 0
    for item in skills:
        status = str(item.get("status") or "unknown")
        status_counts[status] = int(status_counts.get(status, 0)) + 1
        artifacts = item.get("artifacts") if isinstance(item.get("artifacts"), dict) else {}
        promotion = artifacts.get("promotion") if isinstance(artifacts.get("promotion"), dict) else {}
        champion_status = str(promotion.get("status") or "unknown")
        champion_status_counts[champion_status] = int(champion_status_counts.get(champion_status, 0)) + 1
        replay = item.get("replay") if isinstance(item.get("replay"), dict) else {}
        eval_data = item.get("eval") if isinstance(item.get("eval"), dict) else {}
        total_replay += int(replay.get("count", 0) or 0)
        total_rule_outcomes += int(eval_data.get("outcome_count", 0) or 0)
        total_rule_passed += round(float(eval_data.get("pass_rate", 0.0) or 0.0) * int(eval_data.get("outcome_count", 0) or 0))
        total_llm_rules += int(eval_data.get("llm_rule_count", 0) or 0)
        llm_outcome_count = int(eval_data.get("llm_outcome_count", 0) or 0)
        total_llm_rule_outcomes += llm_outcome_count
        total_llm_rule_passed += round(float(eval_data.get("llm_pass_rate", 0.0) or 0.0) * llm_outcome_count)
        candidate_eval = item.get("candidate_eval") if isinstance(item.get("candidate_eval"), dict) else {}
        total_candidate_variants += int(candidate_eval.get("candidate_count", 0) or 0)

    report = {
        "generated_at": _utc_now(),
        "mode": "online_skill_lineage_eval",
        "data_dir": str(root),
        "methodology": {
            "lineage": "group online provenance and usage by skill",
            "replay": "freeze compact online conversation windows as replay samples",
            "rules": "compile deterministic rules and optional LLM judge rules from each active skill's description and instructions",
            "gate": "mark skills incubating, watch, healthy, or unobserved from replay and rule signals",
            "champion": "promote the current active version into a local online-eval champion only when it is healthy and beats the prior champion gate",
        },
        "llm_judge": {
            "enabled": bool(side_query),
            "rule_kind": "llm_binary",
            "response_source": "history_latest_assistant",
        },
        "thresholds": {
            "min_replay_samples": min_replay_samples,
            "min_promotion_tests": min_promotion_tests,
            "min_rule_pass_rate": min_rule_pass_rate,
        },
        "aggregate": {
            "online_ingests": len(provenance_rows),
            "ok": ok_count,
            "ok_rate": _ratio(ok_count, len(provenance_rows)),
            "candidate_events": candidate_events,
            "accepted_events": accepted_events,
            "acceptance_rate": _ratio(accepted_events, candidate_events),
            "actions": action_counts,
            "skills": len(skills),
            "statuses": status_counts,
            "champion_statuses": champion_status_counts,
            "replay_samples": total_replay,
            "rule_outcomes": total_rule_outcomes,
            "rule_pass_rate": _ratio(total_rule_passed, total_rule_outcomes),
            "llm_rules": total_llm_rules,
            "llm_rule_outcomes": total_llm_rule_outcomes,
            "llm_rule_pass_rate": _ratio(total_llm_rule_passed, total_llm_rule_outcomes),
            "candidate_variants": total_candidate_variants,
        },
        "skills": skills,
        "recent_failures": recent_failures[-10:],
    }
    if write_report:
        report_path = root / "online_eval_report.json"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        report["report_file"] = str(report_path)
    return report


def evaluate_online_skill_evolution(
    *,
    min_replay_samples: int = DEFAULT_MIN_REPLAY_SAMPLES,
    min_promotion_tests: int = DEFAULT_MIN_PROMOTION_TESTS,
    min_rule_pass_rate: float = DEFAULT_MIN_RULE_PASS_RATE,
    write_report: bool = True,
    write_artifacts: bool = True,
) -> dict[str, Any]:
    import asyncio

    return asyncio.run(
        _evaluate_online_skill_evolution_core(
            min_replay_samples=min_replay_samples,
            min_promotion_tests=min_promotion_tests,
            min_rule_pass_rate=min_rule_pass_rate,
            write_report=write_report,
            write_artifacts=write_artifacts,
            side_query=None,
            include_llm_rules=False,
        )
    )


async def evaluate_online_skill_evolution_async(
    *,
    side_query: SideQuery | None = None,
    min_replay_samples: int = DEFAULT_MIN_REPLAY_SAMPLES,
    min_promotion_tests: int = DEFAULT_MIN_PROMOTION_TESTS,
    min_rule_pass_rate: float = DEFAULT_MIN_RULE_PASS_RATE,
    write_report: bool = True,
    write_artifacts: bool = True,
) -> dict[str, Any]:
    return await _evaluate_online_skill_evolution_core(
        min_replay_samples=min_replay_samples,
        min_promotion_tests=min_promotion_tests,
        min_rule_pass_rate=min_rule_pass_rate,
        write_report=write_report,
        write_artifacts=write_artifacts,
        side_query=side_query,
        include_llm_rules=side_query is not None,
    )


def _status_rank(status: str) -> int:
    order = {"watch": 0, "incubating": 1, "unobserved": 2, "healthy": 3}
    return order.get(str(status or ""), 9)


def _format_eval_failure_summary(eval_data: dict[str, Any], *, limit: int = 2) -> str:
    failures = eval_data.get("failures") if isinstance(eval_data.get("failures"), list) else []
    parts: list[str] = []
    for failure in failures[: max(1, int(limit))]:
        if not isinstance(failure, dict):
            continue
        rule_id = str(failure.get("rule_id") or "unknown_rule").strip()
        details = failure.get("details") if isinstance(failure.get("details"), dict) else {}
        reason = str(details.get("reason") or details.get("error") or "").strip()
        if not reason:
            reason = "no failure reason recorded"
        parts.append(f"{rule_id}: {reason}")
    if len(failures) > len(parts):
        parts.append(f"... {len(failures) - len(parts)} more")
    return "; ".join(parts)


def format_online_skill_eval(report: dict[str, Any] | None = None) -> str:
    report = report or evaluate_online_skill_evolution()
    aggregate = report.get("aggregate") if isinstance(report.get("aggregate"), dict) else {}
    actions = aggregate.get("actions") if isinstance(aggregate.get("actions"), dict) else {}
    statuses = aggregate.get("statuses") if isinstance(aggregate.get("statuses"), dict) else {}
    champion_statuses = aggregate.get("champion_statuses") if isinstance(aggregate.get("champion_statuses"), dict) else {}
    llm_judge = report.get("llm_judge") if isinstance(report.get("llm_judge"), dict) else {}
    llm_enabled = bool(llm_judge.get("enabled"))

    lines = [
        "Online skill eval:",
        f"  data_dir={report.get('data_dir', '')}",
        (
            "  aggregate: "
            f"ingests={aggregate.get('online_ingests', 0)}, "
            f"ok_rate={_pct(float(aggregate.get('ok_rate', 0) or 0))}, "
            f"candidate_events={aggregate.get('candidate_events', 0)}, "
            f"acceptance_rate={_pct(float(aggregate.get('acceptance_rate', 0) or 0))}, "
            f"replay_samples={aggregate.get('replay_samples', 0)}, "
            f"rule_pass_rate={_pct(float(aggregate.get('rule_pass_rate', 0) or 0))}, "
            f"llm={'on' if llm_enabled else 'off'}, "
            f"llm_rules={aggregate.get('llm_rules', 0)}, "
            f"llm_judgments={aggregate.get('llm_rule_outcomes', 0)}, "
            f"llm_pass_rate={_pct(float(aggregate.get('llm_rule_pass_rate', 0) or 0))}, "
            f"candidates={aggregate.get('candidate_variants', 0)}"
        ),
        (
            "  actions: "
            f"none={actions.get('none', 0)}, add={actions.get('add', 0)}, "
            f"merge={actions.get('merge', 0)}, discard={actions.get('discard', 0)}, "
            f"failed={actions.get('failed', 0)}, denied={actions.get('denied', 0)}"
        ),
    ]
    if statuses:
        lines.append("  statuses: " + ", ".join(f"{key}={statuses[key]}" for key in sorted(statuses)))
    if champion_statuses:
        lines.append("  champion_statuses: " + ", ".join(f"{key}={champion_statuses[key]}" for key in sorted(champion_statuses)))

    skills = report.get("skills") if isinstance(report.get("skills"), list) else []
    if not skills:
        lines.append("  no online skill lineage or usage records found yet")
    else:
        lines.append("  skills:")
        ranked = sorted(
            skills,
            key=lambda item: (
                _status_rank(str(item.get("status") or "")),
                -int((item.get("replay") or {}).get("count", 0) if isinstance(item.get("replay"), dict) else 0),
                str(item.get("skill") or ""),
            ),
        )
        for item in ranked[:20]:
            replay = item.get("replay") if isinstance(item.get("replay"), dict) else {}
            eval_data = item.get("eval") if isinstance(item.get("eval"), dict) else {}
            candidate_eval = item.get("candidate_eval") if isinstance(item.get("candidate_eval"), dict) else {}
            best_candidate = candidate_eval.get("best_candidate") if isinstance(candidate_eval.get("best_candidate"), dict) else {}
            artifacts = item.get("artifacts") if isinstance(item.get("artifacts"), dict) else {}
            promotion = artifacts.get("promotion") if isinstance(artifacts.get("promotion"), dict) else {}
            reasons = "; ".join(str(reason) for reason in item.get("reasons", []) if str(reason).strip())
            failure_summary = _format_eval_failure_summary(eval_data)
            suffix_parts = []
            if reasons:
                suffix_parts.append(reasons)
            if failure_summary:
                suffix_parts.append(f"failures: {failure_summary}")
            suffix = f" - {'; '.join(suffix_parts)}" if suffix_parts else ""
            lines.append(
                "    "
                f"{item.get('skill')}: status={item.get('status')}, "
                f"replay={replay.get('count', 0)} "
                f"(test={replay.get('promotion_test', 0)}), "
                f"rules={eval_data.get('rule_count', 0)}, "
                f"llm_rules={eval_data.get('llm_rule_count', 0)}, "
                f"llm_judgments={eval_data.get('llm_outcome_count', 0)}, "
                f"candidates={candidate_eval.get('candidate_count', 0)}, "
                f"best_candidate_score={float(best_candidate.get('average_score', 0.0) or 0.0):.2f}, "
                f"rule_pass={_pct(float(eval_data.get('pass_rate', 0) or 0))}, "
                f"hard_failures={eval_data.get('hard_failures', 0)}, "
                f"champion={promotion.get('status', 'n/a')}"
                f"{suffix}"
            )
        if len(skills) > 20:
            lines.append(f"    ... {len(skills) - 20} more skill(s) omitted")

    failures = report.get("recent_failures") if isinstance(report.get("recent_failures"), list) else []
    if failures:
        lines.append("  recent failures:")
        for failure in failures[-5:]:
            lines.append(
                "    "
                f"{failure.get('time', '')} {failure.get('action', '')} "
                f"{failure.get('skill', '')}: {failure.get('error', '')}"
            )

    if report.get("report_file"):
        lines.append(f"  report_file={report['report_file']}")
    return "\n".join(lines)


async def format_online_skill_eval_async(
    *,
    side_query: SideQuery | None = None,
    report: dict[str, Any] | None = None,
) -> str:
    if report is None:
        report = await evaluate_online_skill_evolution_async(side_query=side_query)
    return format_online_skill_eval(report)
