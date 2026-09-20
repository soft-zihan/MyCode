from __future__ import annotations

import json
from typing import Any

from agents._utils import (
    stable_hash as _stable_hash,
    ratio as _ratio,
    read_jsonl as _read_jsonl,
    write_jsonl as _write_jsonl,
)
from .eval_rules import (
    SideQuery,
    _evaluate_rule,
    _evaluate_rule_async,
    _parse_json_object,
    _additions_for_failure,
)
from .eval_champion import (
    _lineage_id_for_skill,
    _lineage_dataset_dir,
)

DEFAULT_MIN_REPLAY_SAMPLES = 2
DEFAULT_MIN_PROMOTION_TESTS = 1
DEFAULT_MIN_RULE_PASS_RATE = 0.8
DEFAULT_DEV_SPLIT_RATIO = 0.75


def _latest_message(messages: list[dict[str, Any]], role: str) -> str:
    wanted = str(role or "").strip().lower()
    for item in reversed(list(messages or [])):
        if str(item.get("role") or "").strip().lower() == wanted:
            return str(item.get("content") or "").strip()
    return ""


def _normalized_messages(value: Any) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    if not isinstance(value, list):
        return out
    for item in value:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "").strip().lower()
        content = str(item.get("content") or "").strip()
        if role in {"user", "assistant"} and content:
            out.append({"role": role, "content": content})
    return out


def _active_skill_snapshots() -> dict[str, dict[str, Any]]:
    try:
        from .skills import discover_skills
    except Exception:
        return {}

    snapshots: dict[str, dict[str, Any]] = {}
    for skill in discover_skills():
        name = str(getattr(skill, "name", "") or "").strip()
        if not name:
            continue
        snapshots[name] = {
            "name": name,
            "description": str(getattr(skill, "description", "") or ""),
            "when_to_use": str(getattr(skill, "when_to_use", "") or ""),
            "instructions": str(getattr(skill, "prompt_template", "") or ""),
            "source": str(getattr(skill, "source", "") or ""),
            "skill_dir": str(getattr(skill, "skill_dir", "") or ""),
            "context": str(getattr(skill, "context", "") or ""),
        }
    return snapshots


def _rows_by_skill(provenance_rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in provenance_rows:
        skill = str(row.get("skill") or "").strip()
        if not skill:
            result = row.get("result") if isinstance(row.get("result"), dict) else {}
            skill = str(result.get("skill") or "").strip()
        if skill:
            grouped.setdefault(skill, []).append(row)
    return grouped


def _build_replay_pool(
    skill_name: str,
    rows: list[dict[str, Any]],
    lineage: dict[str, Any],
    *,
    freeze: bool = True,
) -> list[dict[str, Any]]:
    samples: dict[str, dict[str, Any]] = {}

    def add_sample(source: dict[str, Any], source_kind: str) -> None:
        messages = _normalized_messages(source.get("messages"))
        if not messages or not _latest_message(messages, "user"):
            return
        sample_id = _stable_hash(
            {
                "skill": skill_name,
                "messages": messages,
                "latest_user": _latest_message(messages, "user"),
            }
        )
        samples[sample_id] = {
            "sample_id": sample_id,
            "source_type": source_kind,
            "split": "mutate_dev",
            "time": source.get("time", ""),
            "action": source.get("action", ""),
            "ok": bool(source.get("ok", True)),
            "latest_user": _latest_message(messages, "user"),
            "latest_assistant": _latest_message(messages, "assistant"),
            "messages": messages,
        }

    for row in rows:
        add_sample(row, "online_log")
    for source in lineage.get("sources", []) if isinstance(lineage.get("sources"), list) else []:
        if isinstance(source, dict):
            add_sample(source, "online_index")

    ordered = sorted(samples.values(), key=lambda item: (str(item.get("time") or ""), item["sample_id"]), reverse=True)
    if not freeze:
        return _assign_replay_splits(ordered)
    return _freeze_replay_pool(skill_name=skill_name, samples=ordered)


def _split_score(sample_id: str) -> float:
    raw = str(sample_id or "")
    seed = raw[:8] if len(raw) >= 8 else _stable_hash(raw)[:8]
    try:
        return int(seed, 16) / float(16**8 - 1)
    except Exception:
        return 0.0


def _assign_replay_splits(samples: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = [dict(item) for item in samples]
    if len(out) < 2:
        for item in out:
            item["split"] = "mutate_dev"
        return out

    for item in out:
        score = _split_score(str(item.get("sample_id") or ""))
        item["split"] = "mutate_dev" if score < DEFAULT_DEV_SPLIT_RATIO else "promotion_test"

    if not any(item.get("split") == "promotion_test" for item in out):
        out[-1]["split"] = "promotion_test"
    if not any(item.get("split") == "mutate_dev" for item in out):
        out[0]["split"] = "mutate_dev"
    return out


def _freeze_replay_pool(*, skill_name: str, samples: list[dict[str, Any]]) -> list[dict[str, Any]]:
    lineage_id = _lineage_id_for_skill(skill_name)
    path = _lineage_dataset_dir(lineage_id) / "replay_pool.jsonl"
    existing = _read_jsonl(path)
    by_id: dict[str, dict[str, Any]] = {}

    for item in existing:
        sample_id = str(item.get("sample_id") or "").strip()
        if sample_id:
            by_id[sample_id] = dict(item)

    for item in samples:
        sample_id = str(item.get("sample_id") or "").strip()
        if not sample_id:
            continue
        previous = by_id.get(sample_id)
        if previous:
            merged = dict(previous)
            merged.update({key: value for key, value in item.items() if key != "split"})
            by_id[sample_id] = merged
        else:
            by_id[sample_id] = dict(item)

    frozen = sorted(by_id.values(), key=lambda item: (str(item.get("time") or ""), str(item.get("sample_id") or "")), reverse=True)
    frozen = _assign_replay_splits(frozen)
    _write_jsonl(path, frozen)
    return frozen


def _summarize_rule_outcomes(rules: list[dict[str, Any]], samples: list[dict[str, Any]]) -> dict[str, Any]:
    outcomes: list[dict[str, Any]] = []
    for sample in samples:
        response = str(sample.get("latest_assistant") or "")
        for rule in rules:
            outcome = _evaluate_rule(rule, response)
            outcome["sample_id"] = sample.get("sample_id", "")
            outcome["split"] = sample.get("split", "")
            outcome["kind"] = rule.get("kind", "programmatic")
            outcome["score"] = (2.0 if outcome.get("hard") else 1.0) if outcome.get("passed") else 0.0
            outcomes.append(outcome)

    return _summarize_outcomes_from_rows(rules=rules, samples=samples, outcomes=outcomes)


async def _summarize_rule_outcomes_async(
    rules: list[dict[str, Any]],
    samples: list[dict[str, Any]],
    *,
    skill_name: str,
    side_query: SideQuery | None,
) -> dict[str, Any]:
    outcomes: list[dict[str, Any]] = []
    for sample in samples:
        response = str(sample.get("latest_assistant") or "")
        for rule in rules:
            outcome = await _evaluate_rule_async(
                rule,
                response,
                sample=sample,
                skill_name=skill_name,
                side_query=side_query,
            )
            outcome["sample_id"] = sample.get("sample_id", "")
            outcome["split"] = sample.get("split", "")
            outcome["kind"] = rule.get("kind", "programmatic")
            outcome["score"] = (2.0 if outcome.get("hard") else 1.0) if outcome.get("passed") else 0.0
            outcomes.append(outcome)
    return _summarize_outcomes_from_rows(rules=rules, samples=samples, outcomes=outcomes)


def _summarize_outcomes_from_rows(
    *,
    rules: list[dict[str, Any]],
    samples: list[dict[str, Any]],
    outcomes: list[dict[str, Any]],
) -> dict[str, Any]:
    total = len(outcomes)
    passed = sum(1 for item in outcomes if item.get("passed"))
    llm_rules = [rule for rule in rules if str(rule.get("kind") or "programmatic") == "llm_binary"]
    llm_outcomes = [item for item in outcomes if str(item.get("kind") or "programmatic") == "llm_binary"]
    llm_passed = sum(1 for item in llm_outcomes if item.get("passed"))
    total_score = float(sum(float(item.get("score", 0.0) or 0.0) for item in outcomes))
    hard = [item for item in outcomes if item.get("hard")]
    hard_failed = [item for item in hard if not item.get("passed")]
    test = [item for item in outcomes if item.get("split") == "promotion_test"]
    test_passed = sum(1 for item in test if item.get("passed"))
    test_hard_failed = [item for item in test if item.get("hard") and not item.get("passed")]
    by_rule: dict[str, dict[str, Any]] = {}
    for outcome in outcomes:
        rid = str(outcome.get("rule_id") or "unknown")
        item = by_rule.setdefault(rid, {"rule_id": rid, "passed": 0, "total": 0, "hard": bool(outcome.get("hard"))})
        item["total"] = int(item.get("total", 0)) + 1
        if outcome.get("passed"):
            item["passed"] = int(item.get("passed", 0)) + 1
    for item in by_rule.values():
        item["pass_rate"] = _ratio(int(item.get("passed", 0)), int(item.get("total", 0)))

    return {
        "rules": rules,
        "rule_count": len(rules),
        "programmatic_rule_count": len(rules) - len(llm_rules),
        "llm_rule_count": len(llm_rules),
        "outcome_count": total,
        "llm_outcome_count": len(llm_outcomes),
        "llm_pass_rate": _ratio(llm_passed, len(llm_outcomes)),
        "skipped_outcome_count": sum(1 for item in outcomes if item.get("skipped")),
        "passed_rules": passed,
        "total_score": total_score,
        "average_score": _ratio(total_score, max(1, len(samples))),
        "pass_rate": _ratio(passed, total),
        "hard_failures": len(hard_failed),
        "promotion_test_pass_rate": _ratio(test_passed, len(test)),
        "promotion_test_hard_failures": len(test_hard_failed),
        "by_rule": sorted(by_rule.values(), key=lambda item: str(item.get("rule_id") or "")),
        "failures": [
            {
                "sample_id": item.get("sample_id", ""),
                "split": item.get("split", ""),
                "rule_id": item.get("rule_id", ""),
                "label": item.get("label", ""),
                "hard": item.get("hard", False),
                "details": item.get("details", {}),
            }
            for item in outcomes
            if not item.get("passed") and not item.get("skipped")
        ][:20],
        "outcomes": outcomes,
    }


def _snapshot_with_instructions(snapshot: dict[str, Any], *, instructions: str, label: str) -> dict[str, Any]:
    out = dict(snapshot or {})
    out["instructions"] = str(instructions or "")
    out["mutation_label"] = str(label or "")
    return out


def _append_eval_guards(instructions: str, additions: list[str]) -> str:
    body = str(instructions or "").rstrip()
    clean = [str(item or "").strip() for item in additions if str(item or "").strip()]
    if not clean:
        return body
    marker = "## Online Eval Improvement Guards"
    if marker not in body:
        body = (body + "\n\n" if body else "") + marker + "\n"
    for item in clean:
        line = item if item.startswith("- ") else f"- {item}"
        if line not in body:
            body += line + "\n"
    return body


def _candidate_variant_id(*, lineage_id: str, label: str, snapshot: dict[str, Any]) -> str:
    return f"candidate-{_stable_hash({'lineage_id': lineage_id, 'label': label, 'snapshot': snapshot})[:12]}"


def _rule_by_id(rules: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(rule.get("rule_id") or ""): rule for rule in rules if str(rule.get("rule_id") or "")}


def _build_heuristic_candidate_variants(
    *,
    lineage_id: str,
    snapshot: dict[str, Any],
    rules: list[dict[str, Any]],
    rule_summary: dict[str, Any],
    max_variants: int = 4,
) -> list[dict[str, Any]]:
    rule_lookup = _rule_by_id(rules)
    failures = rule_summary.get("failures") if isinstance(rule_summary.get("failures"), list) else []
    if not failures:
        failures = [{"rule_id": str(rule.get("rule_id") or "")} for rule in rules if str(rule.get("kind") or "") != "programmatic"][:2]
    variants: list[dict[str, Any]] = []
    seen_labels: set[str] = set()
    base_instructions = str(snapshot.get("instructions") or "")
    for failure in failures:
        if not isinstance(failure, dict):
            continue
        rid = str(failure.get("rule_id") or "").strip()
        if not rid or rid in seen_labels:
            continue
        rule = rule_lookup.get(rid, {})
        additions = _additions_for_failure(failure, rule)
        if not additions:
            continue
        label = f"heuristic:{rid}"
        new_snapshot = _snapshot_with_instructions(
            snapshot,
            instructions=_append_eval_guards(base_instructions, additions),
            label=label,
        )
        if str(new_snapshot.get("instructions") or "").strip() == base_instructions.strip():
            continue
        variants.append(
            {
                "variant_id": _candidate_variant_id(lineage_id=lineage_id, label=label, snapshot=new_snapshot),
                "label": label,
                "mutation_type": "heuristic",
                "parent_variant_id": "current_active",
                "snapshot": new_snapshot,
                "notes": "; ".join(additions)[:1000],
            }
        )
        seen_labels.add(rid)
        if len(variants) >= max(1, int(max_variants)):
            break
    return variants


async def _build_llm_candidate_variant_async(
    *,
    lineage_id: str,
    snapshot: dict[str, Any],
    rules: list[dict[str, Any]],
    rule_summary: dict[str, Any],
    side_query: SideQuery | None,
) -> dict[str, Any] | None:
    if side_query is None:
        return None
    failures = rule_summary.get("failures") if isinstance(rule_summary.get("failures"), list) else []
    if not failures:
        return None
    system = (
        "You improve a local agent Skill for replay evaluation.\n"
        "Output ONLY strict JSON parseable by json.loads.\n"
        'Schema: {"description": "...", "instructions": "...", "notes": "..."}\n'
        "Make a small durable improvement. Do not invent new capabilities. Preserve the same Skill identity.\n"
    )
    payload = {
        "skill": {
            "name": snapshot.get("name", ""),
            "description": snapshot.get("description", ""),
            "when_to_use": snapshot.get("when_to_use", ""),
            "instructions": snapshot.get("instructions", ""),
        },
        "rules": rules[:6],
        "failures": failures[:4],
    }
    try:
        parsed = _parse_json_object(await side_query(system, json.dumps(payload, ensure_ascii=False)))
    except Exception:
        return None
    instructions = str(parsed.get("instructions") or "").strip()
    if not instructions:
        return None
    label = "llm_mutation"
    new_snapshot = dict(snapshot or {})
    new_snapshot["description"] = str(parsed.get("description") or snapshot.get("description") or "")
    new_snapshot["instructions"] = instructions
    new_snapshot["mutation_label"] = label
    return {
        "variant_id": _candidate_variant_id(lineage_id=lineage_id, label=label, snapshot=new_snapshot),
        "label": label,
        "mutation_type": "llm",
        "parent_variant_id": "current_active",
        "snapshot": new_snapshot,
        "notes": str(parsed.get("notes") or "LLM-guided mutation").strip()[:1000],
    }


async def _generate_variant_response_async(
    *,
    variant: dict[str, Any],
    sample: dict[str, Any],
    side_query: SideQuery | None,
) -> str:
    if side_query is None:
        return ""
    snapshot = variant.get("snapshot") if isinstance(variant.get("snapshot"), dict) else {}
    system = str(snapshot.get("instructions") or snapshot.get("description") or "").strip()
    messages = sample.get("messages") if isinstance(sample.get("messages"), list) else []
    history = "\n\n".join(
        f"[{str(item.get('role') or '').strip()}] {str(item.get('content') or '').strip()}"
        for item in messages
        if isinstance(item, dict) and str(item.get("content") or "").strip()
    )
    user = (
        "Replay the following conversation with the candidate Skill instructions already injected.\n\n"
        f"Conversation:\n{history}\n\n"
        "Respond to the latest user message. Return only the assistant response."
    )
    try:
        return str(await side_query(system, user) or "").strip()
    except Exception as exc:
        return f"[candidate_response_failed: {exc}]"


async def _evaluate_generated_variant_async(
    *,
    variant: dict[str, Any],
    samples: list[dict[str, Any]],
    rules: list[dict[str, Any]],
    side_query: SideQuery | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    outputs: list[dict[str, Any]] = []
    outcomes: list[dict[str, Any]] = []
    snapshot = variant.get("snapshot") if isinstance(variant.get("snapshot"), dict) else {}
    skill_name = str(snapshot.get("name") or variant.get("label") or "")
    for sample in samples:
        response = await _generate_variant_response_async(variant=variant, sample=sample, side_query=side_query)
        outputs.append(
            {
                "sample_id": sample.get("sample_id", ""),
                "variant_id": variant.get("variant_id", ""),
                "split": sample.get("split", ""),
                "source_type": sample.get("source_type", ""),
                "latest_user": sample.get("latest_user", ""),
                "response_source": "candidate_generated",
                "response_text": response,
            }
        )
        for rule in rules:
            outcome = await _evaluate_rule_async(
                rule,
                response,
                sample=sample,
                skill_name=skill_name,
                side_query=side_query,
            )
            outcome["sample_id"] = sample.get("sample_id", "")
            outcome["split"] = sample.get("split", "")
            outcome["kind"] = rule.get("kind", "programmatic")
            outcome["variant_id"] = variant.get("variant_id", "")
            outcome["score"] = (2.0 if outcome.get("hard") else 1.0) if outcome.get("passed") else 0.0
            outcomes.append(outcome)
    summary = _summarize_outcomes_from_rows(rules=rules, samples=samples, outcomes=outcomes)
    return outputs, outcomes, summary


def _variant_summary_from_eval(variant: dict[str, Any], summary: dict[str, Any], sample_count: int) -> dict[str, Any]:
    return {
        "variant_id": variant.get("variant_id", ""),
        "label": variant.get("label", ""),
        "mutation_type": variant.get("mutation_type", ""),
        "parent_variant_id": variant.get("parent_variant_id", ""),
        "sample_count": int(sample_count or 0),
        "total_score": float(summary.get("total_score", 0.0) or 0.0),
        "average_score": float(summary.get("average_score", 0.0) or 0.0),
        "hard_failures": int(summary.get("hard_failures", 0) or 0),
        "passed_rules": int(summary.get("passed_rules", 0) or 0),
        "total_rules": int(summary.get("outcome_count", 0) or 0),
        "rule_pass_rate": float(summary.get("pass_rate", 0.0) or 0.0),
        "notes": str(variant.get("notes") or ""),
    }


async def _build_candidate_eval_bundle_async(
    *,
    lineage_id: str,
    snapshot: dict[str, Any],
    replay_pool: list[dict[str, Any]],
    rules: list[dict[str, Any]],
    rule_summary: dict[str, Any],
    side_query: SideQuery | None,
) -> dict[str, Any]:
    if side_query is None or not replay_pool:
        return {}
    variants = _build_heuristic_candidate_variants(
        lineage_id=lineage_id,
        snapshot=snapshot,
        rules=rules,
        rule_summary=rule_summary,
    )
    llm_variant = await _build_llm_candidate_variant_async(
        lineage_id=lineage_id,
        snapshot=snapshot,
        rules=rules,
        rule_summary=rule_summary,
        side_query=side_query,
    )
    if llm_variant is not None:
        variants.append(llm_variant)
    deduped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for variant in variants:
        key = _stable_hash((variant.get("snapshot") or {}).get("instructions", ""))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(variant)
    dev_samples = [sample for sample in replay_pool if sample.get("split") == "mutate_dev"] or list(replay_pool)
    test_samples = [sample for sample in replay_pool if sample.get("split") == "promotion_test"]
    outputs: list[dict[str, Any]] = []
    judgments: list[dict[str, Any]] = []
    scored: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]] = []
    for variant in deduped[:4]:
        variant_outputs, variant_outcomes, variant_summary = await _evaluate_generated_variant_async(
            variant=variant,
            samples=dev_samples,
            rules=rules,
            side_query=side_query,
        )
        outputs.extend(variant_outputs)
        judgments.extend(variant_outcomes)
        scored.append((variant, variant_summary, _variant_summary_from_eval(variant, variant_summary, len(dev_samples))))
    if not scored:
        return {}
    scored.sort(
        key=lambda item: (
            float(item[1].get("average_score", 0.0) or 0.0),
            -int(item[1].get("hard_failures", 0) or 0),
        ),
        reverse=True,
    )
    best_variant, best_dev_eval, best_dev_summary = scored[0]
    best_test_summary: dict[str, Any] = {}
    if test_samples:
        test_outputs, test_outcomes, test_eval = await _evaluate_generated_variant_async(
            variant=best_variant,
            samples=test_samples,
            rules=rules,
            side_query=side_query,
        )
        outputs.extend(test_outputs)
        judgments.extend(test_outcomes)
        best_test_summary = _variant_summary_from_eval(best_variant, test_eval, len(test_samples))
    return {
        "candidate_variants": deduped[:4],
        "variant_summaries": [item[2] for item in scored],
        "best_variant": best_variant,
        "best_dev_summary": best_dev_summary,
        "best_test_summary": best_test_summary,
        "outputs": outputs,
        "judgments": judgments,
    }
