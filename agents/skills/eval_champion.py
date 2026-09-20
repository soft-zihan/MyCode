from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from agents._utils import (
    utc_now as _utc_now,
    read_json as _read_json,
    write_json as _write_json,
    write_jsonl as _write_jsonl,
    stable_hash as _stable_hash,
)
from agents.core.workspace import get_workspace
from .skill_file_ops import get_evolution_dir

ONLINE_EVAL_DIR = "online-eval"

DEFAULT_MIN_SCORE_DELTA = 0.01


def _lineage_id_for_skill(skill_name: str) -> str:
    return "skill-" + _stable_hash({"name": str(skill_name or "").strip()})[:16]


def _online_eval_root() -> Path:
    return get_evolution_dir() / ONLINE_EVAL_DIR


def _lineage_dataset_dir(lineage_id: str) -> Path:
    return _online_eval_root() / "datasets" / lineage_id


def _lineage_eval_dir(lineage_id: str) -> Path:
    return _online_eval_root() / "evals" / lineage_id


def _lineage_run_dir(lineage_id: str, run_id: str) -> Path:
    return _online_eval_root() / "runs" / lineage_id / run_id


def _champions_registry_path() -> Path:
    return _online_eval_root() / "champions.json"


def _lineage_champion_dir(lineage_id: str) -> Path:
    return _online_eval_root() / "champions" / lineage_id


def _run_id(lineage_id: str) -> str:
    return f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{str(lineage_id or '')[-6:]}"


def _variant_summary(
    *,
    skill_name: str,
    snapshot: dict[str, Any],
    rule_summary: dict[str, Any],
    replay_pool: list[dict[str, Any]],
) -> dict[str, Any]:
    version = str(snapshot.get("version") or snapshot.get("current_version") or "active").strip() or "active"
    return {
        "variant_id": f"current-{_stable_hash({'skill': skill_name, 'version': version})[:10]}",
        "label": "current_active",
        "mutation_type": "active",
        "skill": skill_name,
        "version": version,
        "sample_count": len(replay_pool),
        "total_score": float(rule_summary.get("total_score", 0.0) or 0.0),
        "average_score": float(rule_summary.get("average_score", 0.0) or 0.0),
        "hard_failures": int(rule_summary.get("hard_failures", 0) or 0),
        "passed_rules": int(rule_summary.get("passed_rules", 0) or 0),
        "total_rules": int(rule_summary.get("outcome_count", 0) or 0),
        "rule_pass_rate": float(rule_summary.get("pass_rate", 0.0) or 0.0),
    }


def _load_champion(lineage_id: str) -> dict[str, Any]:
    registry = _read_json(_champions_registry_path(), {"champions": {}})
    if not isinstance(registry, dict):
        return {}
    champions = registry.get("champions") if isinstance(registry.get("champions"), dict) else {}
    item = champions.get(lineage_id)
    return dict(item or {}) if isinstance(item, dict) else {}


def _set_champion(lineage_id: str, payload: dict[str, Any]) -> None:
    path = _champions_registry_path()
    registry = _read_json(path, {"version": 1, "champions": {}})
    if not isinstance(registry, dict):
        registry = {"version": 1, "champions": {}}
    champions = registry.setdefault("champions", {})
    if not isinstance(champions, dict):
        champions = {}
        registry["champions"] = champions
    champions[lineage_id] = payload
    _write_json(path, registry)
    champion_dir = _lineage_champion_dir(lineage_id)
    _write_json(champion_dir / "champion.json", payload)
    snapshot = payload.get("snapshot") if isinstance(payload.get("snapshot"), dict) else {}
    if snapshot:
        _write_champion_skill_file(champion_dir / "SKILL.md", snapshot)


def _write_champion_skill_file(path: Path, snapshot: dict[str, Any]) -> None:
    name = str(snapshot.get("name") or "unnamed-skill").strip()
    description = str(snapshot.get("description") or "").strip()
    when_to_use = str(snapshot.get("when_to_use") or "").strip()
    instructions = str(snapshot.get("instructions") or "").strip()
    lines = ["---", f"name: {name}"]
    if description:
        lines.append(f"description: {description}")
    if when_to_use:
        lines.append(f"when-to-use: {when_to_use}")
    lines.extend(["---", "", instructions])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def _auto_activate_enabled() -> bool:
    """检查是否启用 champion 自动激活。

    环境变量 MYCODE_SKILL_AUTO_ACTIVATE=1 启用。
    默认关闭（观察模式），需显式开启。
    """
    import os
    return os.environ.get("MYCODE_SKILL_AUTO_ACTIVATE", "").strip() in ("1", "true", "yes")


def _activate_champion(skill_name: str, snapshot: dict[str, Any], lineage_id: str) -> dict[str, Any]:
    """将 champion 版本激活为 active skill。

    复制 champion SKILL.md 到 active skill 位置，并记录激活事件。

    Returns:
        {"activated": bool, "path": str, "reason": str}
    """
    from agents.observability.trace import trace_event

    active_path = _find_active_skill_path(skill_name)
    if not active_path:
        return {
            "activated": False,
            "path": "",
            "reason": f"active skill file not found for {skill_name}",
        }

    backup_path = active_path.with_suffix(".md.bak")
    if active_path.is_file():
        backup_path.write_text(active_path.read_text(encoding="utf-8"), encoding="utf-8")

    _write_champion_skill_file(active_path, snapshot)

    trace_event(
        "champion.activate",
        metadata={
            "skill": skill_name,
            "lineage_id": lineage_id,
            "path": str(active_path),
            "backup_path": str(backup_path),
        },
    )

    return {
        "activated": True,
        "path": str(active_path),
        "reason": "champion promoted and activated",
    }


def _find_active_skill_path(skill_name: str) -> Path | None:
    """查找 active skill 文件路径。"""
    project_path = get_workspace() / ".mycode" / "skills" / skill_name / "SKILL.md"
    if project_path.is_file():
        return project_path

    user_path = Path.home() / ".mycode" / "skills" / skill_name / "SKILL.md"
    if user_path.is_file():
        return user_path

    return project_path


def _check_historical_retention(
    *,
    candidate_score: float,
    champion_score: float,
    candidate_hard: int,
    champion_hard: int,
) -> dict[str, Any]:
    """检查候选版本是否在旧 replay 样本上退化（historical retention check）。

    借鉴 HCL (arXiv:2605.09998) 的 guarded harness evolution：
    Continual Evaluator 检查 current improvement + historical retention + validity。

    Returns:
        {"passed": bool, "reason": str, "delta": float}
    """
    delta = candidate_score - champion_score
    if delta < 0:
        return {
            "passed": False,
            "reason": f"candidate regresses on historical replay (delta={delta:.4f})",
            "delta": delta,
        }
    if candidate_hard > champion_hard:
        return {
            "passed": False,
            "reason": f"candidate introduces {candidate_hard - champion_hard} new hard failure(s)",
            "delta": delta,
        }
    return {
        "passed": True,
        "reason": "candidate preserves or improves historical replay performance",
        "delta": delta,
    }


def _promotion_decision(
    *,
    status: str,
    candidate: dict[str, Any],
    champion: dict[str, Any],
    min_score_delta: float = DEFAULT_MIN_SCORE_DELTA,
    auto_activate: bool = False,
) -> dict[str, Any]:
    if status in {"unobserved", "incubating"}:
        return {
            "promoted": False,
            "status": status,
            "reason": "not enough usable replay signal for champion promotion",
            "champion_before": champion,
            "candidate": candidate,
            "min_score_delta": min_score_delta,
            "auto_activate": auto_activate,
        }
    if status == "watch":
        return {
            "promoted": False,
            "status": "rejected",
            "reason": "candidate is under watch",
            "champion_before": champion,
            "candidate": candidate,
            "min_score_delta": min_score_delta,
            "auto_activate": auto_activate,
        }

    previous_summary = champion.get("summary") if isinstance(champion.get("summary"), dict) else {}
    if not previous_summary:
        return {
            "promoted": True,
            "status": "active_champion",
            "reason": "first healthy candidate for this lineage",
            "champion_before": {},
            "candidate": candidate,
            "min_score_delta": min_score_delta,
            "auto_activate": auto_activate,
            "retention_check": {"passed": True, "reason": "no previous champion to regress against"},
        }

    candidate_score = float(candidate.get("average_score", 0.0) or 0.0)
    champion_score = float(previous_summary.get("average_score", 0.0) or 0.0)
    candidate_hard = int(candidate.get("hard_failures", 0) or 0)
    champion_hard = int(previous_summary.get("hard_failures", 0) or 0)

    retention = _check_historical_retention(
        candidate_score=candidate_score,
        champion_score=champion_score,
        candidate_hard=candidate_hard,
        champion_hard=champion_hard,
    )

    if not retention["passed"]:
        return {
            "promoted": False,
            "status": "rejected",
            "reason": f"failed retention check: {retention['reason']}",
            "champion_before": champion,
            "candidate": candidate,
            "min_score_delta": min_score_delta,
            "auto_activate": auto_activate,
            "retention_check": retention,
        }

    promoted = bool(
        candidate_score >= champion_score + float(min_score_delta)
        and candidate_hard <= champion_hard
    )
    return {
        "promoted": promoted,
        "status": "active_champion" if promoted else "rejected",
        "reason": (
            "candidate beats current champion on average score without more hard failures"
            if promoted
            else "candidate does not beat current champion promotion gate"
        ),
        "champion_before": champion,
        "candidate": candidate,
        "min_score_delta": min_score_delta,
        "auto_activate": auto_activate,
        "retention_check": retention,
    }


def _persist_eval_artifacts(
    *,
    skill_name: str,
    snapshot: dict[str, Any],
    replay_pool: list[dict[str, Any]],
    rules: list[dict[str, Any]],
    rule_summary: dict[str, Any],
    status: str,
    reasons: list[str],
    candidate_bundle: dict[str, Any] | None = None,
) -> dict[str, Any]:
    lineage_id = _lineage_id_for_skill(skill_name)
    eval_dir = _lineage_eval_dir(lineage_id)
    _write_json(
        eval_dir / "eval_spec.json",
        {
            "lineage_id": lineage_id,
            "skill": skill_name,
            "rules": rules,
            "response_source": "history_latest_assistant",
        },
    )

    run_id = _run_id(lineage_id)
    run_dir = _lineage_run_dir(lineage_id, run_id)
    outputs = [
        {
            "sample_id": sample.get("sample_id", ""),
            "variant_id": "current_active",
            "split": sample.get("split", ""),
            "source_type": sample.get("source_type", ""),
            "latest_user": sample.get("latest_user", ""),
            "response_source": "history_latest_assistant",
            "response_text": sample.get("latest_assistant", ""),
        }
        for sample in replay_pool
    ]
    bundle = candidate_bundle if isinstance(candidate_bundle, dict) else {}
    outputs.extend(list(bundle.get("outputs") or []))
    judgments = [
        {
            "sample_id": outcome.get("sample_id", ""),
            "variant_id": "current_active",
            "split": outcome.get("split", ""),
            "rule_id": outcome.get("rule_id", ""),
            "label": outcome.get("label", ""),
            "kind": outcome.get("kind", "programmatic"),
            "hard": bool(outcome.get("hard")),
            "passed": bool(outcome.get("passed")),
            "score": float(outcome.get("score", 0.0) or 0.0),
            "details": outcome.get("details", {}),
        }
        for outcome in list(rule_summary.get("outcomes") or [])
        if isinstance(outcome, dict)
    ]
    for outcome in list(bundle.get("judgments") or []):
        if not isinstance(outcome, dict):
            continue
        judgments.append(
            {
                "sample_id": outcome.get("sample_id", ""),
                "variant_id": outcome.get("variant_id", ""),
                "split": outcome.get("split", ""),
                "rule_id": outcome.get("rule_id", ""),
                "label": outcome.get("label", ""),
                "kind": outcome.get("kind", "programmatic"),
                "hard": bool(outcome.get("hard")),
                "passed": bool(outcome.get("passed")),
                "score": float(outcome.get("score", 0.0) or 0.0),
                "details": outcome.get("details", {}),
            }
        )
    _write_jsonl(run_dir / "outputs.jsonl", outputs)
    _write_jsonl(run_dir / "judgments.jsonl", judgments)

    candidate_summary = _variant_summary(
        skill_name=skill_name,
        snapshot=snapshot,
        rule_summary=rule_summary,
        replay_pool=replay_pool,
    )
    promotion_candidate = candidate_summary
    promotion_snapshot = snapshot
    best_variant = bundle.get("best_variant") if isinstance(bundle.get("best_variant"), dict) else {}
    best_dev_summary = bundle.get("best_dev_summary") if isinstance(bundle.get("best_dev_summary"), dict) else {}
    best_test_summary = bundle.get("best_test_summary") if isinstance(bundle.get("best_test_summary"), dict) else {}
    candidate_beats_current = bool(
        best_dev_summary
        and float(best_dev_summary.get("average_score", 0.0) or 0.0)
        >= float(candidate_summary.get("average_score", 0.0) or 0.0) + DEFAULT_MIN_SCORE_DELTA
        and int(best_dev_summary.get("hard_failures", 0) or 0) <= int(candidate_summary.get("hard_failures", 0) or 0)
    )
    if best_variant and best_test_summary and candidate_beats_current:
        promotion_candidate = dict(best_test_summary)
        promotion_snapshot = best_variant.get("snapshot") if isinstance(best_variant.get("snapshot"), dict) else snapshot
    champion_before = _load_champion(lineage_id)
    promotion = _promotion_decision(
        status=status,
        candidate=promotion_candidate,
        champion=champion_before,
        auto_activate=_auto_activate_enabled(),
    )
    if promotion.get("promoted"):
        _set_champion(
            lineage_id,
            {
                "lineage_id": lineage_id,
                "skill": skill_name,
                "snapshot": promotion_snapshot,
                "summary": promotion_candidate,
                "promotion": promotion,
                "updated_at": _utc_now(),
            },
        )
        if promotion.get("auto_activate"):
            _activate_champion(skill_name, promotion_snapshot, lineage_id)

    summary = {
        "run_id": run_id,
        "lineage_id": lineage_id,
        "skill": skill_name,
        "status": status,
        "reasons": reasons,
        "candidate": candidate_summary,
        "candidate_variants": [
            {
                key: value
                for key, value in dict(variant).items()
                if key != "snapshot"
            }
            for variant in list(bundle.get("candidate_variants") or [])
            if isinstance(variant, dict)
        ],
        "variant_summaries": list(bundle.get("variant_summaries") or []),
        "best_candidate": dict(bundle.get("best_dev_summary") or {}),
        "promotion": promotion,
        "replay_counts": {
            "total": len(replay_pool),
            "mutate_dev": sum(1 for item in replay_pool if item.get("split") == "mutate_dev"),
            "promotion_test": sum(1 for item in replay_pool if item.get("split") == "promotion_test"),
        },
        "artifacts": {
            "dataset": str(_lineage_dataset_dir(lineage_id) / "replay_pool.jsonl"),
            "eval_spec": str(eval_dir / "eval_spec.json"),
            "outputs": str(run_dir / "outputs.jsonl"),
            "judgments": str(run_dir / "judgments.jsonl"),
        },
    }
    _write_json(run_dir / "summary.json", summary)
    return {
        "run_id": run_id,
        "run_dir": str(run_dir),
        "dataset_file": str(_lineage_dataset_dir(lineage_id) / "replay_pool.jsonl"),
        "eval_spec_file": str(eval_dir / "eval_spec.json"),
        "champion_file": str(_lineage_champion_dir(lineage_id) / "champion.json"),
        "promotion": promotion,
    }
