from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Awaitable, Callable


SideQuery = Callable[[str, str], Awaitable[str]]
ConfirmWrite = Callable[[str], Awaitable[bool]]


@dataclass
class OnlineSkillCandidate:
    name: str
    description: str
    when_to_use: str = ""
    instructions: str = ""
    evidence: str = ""
    tags: list[str] = field(default_factory=list)
    kind: str = "skill"
    rule_text: str = ""


def _parse_json_object(text: str) -> dict[str, Any]:
    raw = str(text or "").strip()
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except Exception:
        pass
    start = raw.find("{")
    end = raw.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(raw[start : end + 1])
        except Exception:
            return {}
    return {}


def _normalize_identity(text: str) -> str:
    raw = re.sub(r"[^a-zA-Z0-9\u4e00-\u9fff]+", " ", str(text or "").lower())
    return re.sub(r"\s+", " ", raw).strip()


def _candidate_search_text(candidate: OnlineSkillCandidate) -> str:
    return "\n".join(
        [
            candidate.name,
            candidate.description,
            candidate.when_to_use,
            candidate.instructions,
            " ".join(candidate.tags),
        ]
    )


def _coerce_candidate(obj: dict[str, Any]) -> OnlineSkillCandidate | None:
    name = str(obj.get("name") or "").strip()
    description = str(obj.get("description") or "").strip()
    kind = str(obj.get("kind") or "skill").strip().lower()

    rule_text = str(obj.get("rule_text") or "").strip()
    instructions = str(obj.get("instructions") or obj.get("prompt") or "").strip()

    if kind == "rule":
        if not name or not description or not rule_text:
            return None
    else:
        if not name or not description or not instructions:
            return None

    tags_raw = obj.get("tags") or []
    if isinstance(tags_raw, str):
        tags = [part.strip() for part in re.split(r"[,，]", tags_raw) if part.strip()]
    elif isinstance(tags_raw, list):
        tags = [str(part).strip() for part in tags_raw if str(part).strip()]
    else:
        tags = []
    return OnlineSkillCandidate(
        name=name,
        description=description,
        when_to_use=str(obj.get("when_to_use") or obj.get("when-to-use") or "").strip(),
        instructions=instructions,
        evidence=str(obj.get("evidence") or "").strip(),
        tags=tags[:8],
        kind=kind,
        rule_text=rule_text,
    )


async def extract_online_skill_candidate(
    *,
    messages: list[dict[str, Any]],
    side_query: SideQuery,
    retrieved_reference: dict[str, Any] | None = None,
    hint: str = "",
) -> OnlineSkillCandidate | None:
    system = (
        "You are MyCode's online Experience Extractor.\n"
        "Extract at most ONE reusable experience from a live conversation window.\n"
        "Output ONLY strict JSON: {\"experiences\": []} or {\"experiences\": [{...}]}.\n\n"
        "Each experience must have a \"kind\" field: \"skill\" or \"rule\".\n\n"
        "Kind classification:\n"
        "- \"rule\": Global constraints that should ALWAYS apply. No trigger condition needed.\n"
        "  Examples: \"Always use type hints\", \"Never commit secrets\", \"Use Chinese for comments\",\n"
        "  \"Prefer functional style\", \"No eval() allowed\".\n"
        "  Rule characteristics: short (<200 chars), unconditional, applies to all tasks.\n\n"
        "- \"skill\": Conditional workflows triggered by specific situations.\n"
        "  Examples: \"When user asks to commit, use conventional commits format\",\n"
        "  \"When reviewing PR, check for security issues\", \"When running tests, check permissions first\".\n"
        "  Skill characteristics: multi-step workflow, has when_to_use trigger, needs version control.\n\n"
        "Candidate fields:\n"
        "- kind: \"skill\" or \"rule\"\n"
        "- name: short identifier\n"
        "- description: what this experience captures\n"
        "- when_to_use: (for skill only) trigger condition\n"
        "- instructions: (for skill) detailed workflow steps\n"
        "- rule_text: (for rule only) concise rule statement (<200 chars)\n"
        "- evidence: why this was extracted\n"
        "- tags: relevant keywords\n\n"
        "Extraction rules:\n"
        "- USER turns are the primary evidence. Assistant turns are context only.\n"
        "- A next user feedback turn may confirm, reject, or refine the prior assistant behavior.\n"
        "- Do not extract assistant-only guesses, weak confirmations, one-off task payload, secrets, project facts, URLs, account IDs, exact dates, or temporary parameters.\n"
        "- Extract only durable workflow, output policy, implementation preference, correction, or repeated constraint likely useful for future similar tasks.\n"
        "- Remove entity names and runtime-specific payload; use placeholders where needed.\n"
        "- retrieved_reference is identity context only; never treat it as new user evidence.\n"
        "- If evidence is weak, generic, or low-value, return {\"experiences\": []}.\n"
    )
    payload = {
        "messages": messages,
        "hint": hint,
        "retrieved_reference": retrieved_reference or None,
    }
    parsed = _parse_json_object(await side_query(system, json.dumps(payload, ensure_ascii=False)))
    experiences = parsed.get("experiences")
    if not isinstance(experiences, list) or not experiences:
        return None
    first = experiences[0]
    if not isinstance(first, dict):
        return None
    return _coerce_candidate(first)


def _exact_identity_match(candidate: OnlineSkillCandidate, skills: list[Any]) -> str:
    candidate_ids = {
        _normalize_identity(candidate.name),
        _normalize_identity(candidate.description),
        _normalize_identity(candidate.when_to_use),
    }
    candidate_ids.discard("")
    for skill in skills:
        skill_ids = {
            _normalize_identity(getattr(skill, "name", "")),
            _normalize_identity(getattr(skill, "description", "")),
            _normalize_identity(getattr(skill, "when_to_use", "") or ""),
        }
        skill_ids.discard("")
        if candidate_ids & skill_ids:
            return getattr(skill, "name", "")
    return ""


async def maintain_online_skill_candidate(
    *,
    candidate: OnlineSkillCandidate,
    side_query: SideQuery,
    retrieved_reference: dict[str, Any] | None = None,
    confirm_write: ConfirmWrite | None = None,
    target: str = "project",
) -> dict[str, Any]:
    from .skills import create_skill, discover_skills, evolve_skill, retrieve_relevant_skills

    skills = discover_skills()
    exact_target = _exact_identity_match(candidate, skills)
    similar_hits = retrieve_relevant_skills(_candidate_search_text(candidate), limit=8, min_score=0.03)
    top_reference_name = str((retrieved_reference or {}).get("name") or "").strip()

    system = (
        "You are MyCode's online Skill Set Manager.\n"
        "Decide whether a candidate should add a new skill, merge into an existing skill, or be discarded.\n"
        "Output ONLY strict JSON.\n\n"
        "Schema:\n"
        "{\"action\":\"add|merge|discard\",\"target_skill\":\"existing name for merge\","
        "\"reason\":\"short reason\",\"merged_description\":\"optional\","
        "\"merged_when_to_use\":\"optional\",\"merged_instructions\":\"optional full merged SKILL.md body\"}\n\n"
        "Rules:\n"
        "- Prefer merge over add when the same capability already exists.\n"
        "- Discard if the candidate duplicates an existing shared/project skill and adds no user-specific durable improvement.\n"
        "- If merging, synthesize a complete merged instruction body, preserving useful existing guidance and adding only durable new guidance.\n"
        "- Do not preserve one-off payload, secrets, transient project facts, URLs, exact dates, or assistant-only claims.\n"
    )
    payload = {
        "candidate": asdict(candidate),
        "exact_identity_target": exact_target,
        "retrieved_reference": retrieved_reference or None,
        "similar_skills": similar_hits,
        "existing_skills": [
            {
                "name": getattr(skill, "name", ""),
                "description": getattr(skill, "description", ""),
                "when_to_use": getattr(skill, "when_to_use", "") or "",
                "source": getattr(skill, "source", ""),
                "context": getattr(skill, "context", ""),
                "instructions": (getattr(skill, "prompt_template", "") or "")[:6000],
            }
            for skill in skills[:80]
        ],
    }

    decision = _parse_json_object(await side_query(system, json.dumps(payload, ensure_ascii=False)))
    action = str(decision.get("action") or "").strip().lower()
    target_skill = str(decision.get("target_skill") or "").strip()

    if exact_target:
        action = "merge"
        target_skill = exact_target
    elif action == "add" and similar_hits:
        top = similar_hits[0]
        if float(top.get("score", 0.0)) >= 0.55:
            action = "merge"
            target_skill = str(top.get("name") or "")
    elif action == "merge" and not target_skill:
        target_skill = top_reference_name

    if action not in {"add", "merge", "discard"}:
        action = "discard"

    if action == "discard":
        return {"ok": True, "action": "discard", "skill": "", "decision": decision}

    write_summary = f"online skill evolution: {action} {target_skill or candidate.name}"
    if confirm_write is not None and not await confirm_write(write_summary):
        return {
            "ok": False,
            "action": f"{action}_denied",
            "skill": target_skill or candidate.name,
            "error": "permission denied",
            "decision": decision,
        }

    if action == "merge":
        target_skill = target_skill or top_reference_name
        if not target_skill:
            return {"ok": False, "action": "merge", "error": "missing target_skill", "decision": decision}
        result = evolve_skill(
            skill_name=target_skill,
            lesson=candidate.evidence or candidate.description,
            rationale=str(decision.get("reason") or "Online maintainer merge"),
            target="active",
            instructions=str(decision.get("merged_instructions") or candidate.instructions),
            description=str(decision.get("merged_description") or ""),
            when_to_use=str(decision.get("merged_when_to_use") or candidate.when_to_use),
            tags=candidate.tags,
        )
        return {"action": "merge", "candidate": asdict(candidate), "decision": decision, **result}

    result = create_skill(
        name=candidate.name,
        description=candidate.description,
        instructions=candidate.instructions,
        when_to_use=candidate.when_to_use,
        target=target,
        context="inline",
        user_invocable=False,
        evidence=candidate.evidence,
        actor="online",
        tags=candidate.tags,
    )
    return {"action": "add", "candidate": asdict(candidate), "decision": decision, **result}


async def online_ingest(
    *,
    messages: list[dict[str, Any]],
    side_query: SideQuery,
    retrieved_reference: dict[str, Any] | None = None,
    hint: str = "",
    confirm_write: ConfirmWrite | None = None,
    target: str = "project",
) -> dict[str, Any]:
    from .skills import record_online_provenance
    from agents.observability.trace import trace_span

    with trace_span("skill.extract") as span:
        try:
            candidate = await extract_online_skill_candidate(
                messages=messages,
                side_query=side_query,
                retrieved_reference=retrieved_reference,
                hint=hint,
            )
        except Exception as exc:
            result = {"ok": False, "action": "failed", "error": str(exc)}
            span.add_metadata(action="failed", error=str(exc))
            span.record_error(exc)
            record_online_provenance(
                action="failed",
                result=result,
                messages=messages,
                retrieved_reference=retrieved_reference,
                error=str(exc),
            )
            return result

        if candidate is None:
            result = {"ok": True, "action": "none"}
            span.add_metadata(action="none")
            record_online_provenance(
                action="none",
                result=result,
                messages=messages,
                retrieved_reference=retrieved_reference,
            )
            return result

        span.add_metadata(candidate_kind=candidate.kind, candidate_name=candidate.name)

        if candidate.kind == "rule":
            try:
                result = await _maintain_rule_candidate(
                    candidate=candidate,
                    confirm_write=confirm_write,
                )
            except Exception as exc:
                result = {"ok": False, "action": "failed", "skill": candidate.name, "error": str(exc)}
                span.record_error(exc)
        else:
            try:
                result = await maintain_online_skill_candidate(
                    candidate=candidate,
                    side_query=side_query,
                    retrieved_reference=retrieved_reference,
                    confirm_write=confirm_write,
                    target=target,
                )
            except Exception as exc:
                result = {"ok": False, "action": "failed", "skill": candidate.name, "error": str(exc)}
                span.record_error(exc)

        decision = result.get("decision")
        metadata: dict[str, Any] = {
            "action": str(result.get("action") or "none"),
            "skill_name": str(result.get("skill") or candidate.name),
            "success": bool(result.get("ok")),
        }
        if isinstance(decision, dict) and decision.get("action"):
            metadata["decision_action"] = str(decision["action"])
        if not result.get("ok") and result.get("error"):
            metadata["error"] = str(result["error"])
        span.add_metadata(**metadata)

        record_online_provenance(
            action=str(result.get("action") or "none"),
            skill_name=str(result.get("skill") or candidate.name),
            result=result,
            messages=messages,
            retrieved_reference=retrieved_reference,
            decision=decision if isinstance(decision, dict) else None,
            error="" if result.get("ok") else str(result.get("error") or ""),
        )
        return result


async def _maintain_rule_candidate(
    *,
    candidate: OnlineSkillCandidate,
    confirm_write: ConfirmWrite | None = None,
) -> dict[str, Any]:
    """Handle Rule candidate: create or update RULES.md."""
    from .rule_file_ops import create_rule, evolve_rule, load_rules

    current_rules = load_rules()

    rule_text = candidate.rule_text.strip()
    existing_similar = ""
    if current_rules:
        for line in current_rules.split("\n"):
            line = line.strip().lstrip("- ").strip()
            if line and (rule_text in line or line in rule_text):
                existing_similar = line
                break

    write_summary = f"online rule evolution: {'evolve' if existing_similar else 'create'} {candidate.name}"
    if confirm_write is not None and not await confirm_write(write_summary):
        return {
            "ok": False,
            "action": f"{'evolve' if existing_similar else 'create'}_denied",
            "skill": candidate.name,
            "error": "permission denied",
        }

    if existing_similar:
        result = evolve_rule(
            old_rule_text=existing_similar,
            new_rule_text=rule_text,
            category=_infer_category(candidate),
            evidence=candidate.evidence,
            rationale=f"Online rule evolution: {candidate.description}",
        )
        return {"action": "evolve", "skill": candidate.name, "kind": "rule", **result}
    else:
        result = create_rule(
            rule_text=rule_text,
            category=_infer_category(candidate),
            evidence=candidate.evidence,
        )
        return {"action": "create", "skill": candidate.name, "kind": "rule", **result}


def _infer_category(candidate: OnlineSkillCandidate) -> str:
    """Infer rule category from candidate tags."""
    tags_lower = [t.lower() for t in candidate.tags]
    if any(t in tags_lower for t in ["style", "format", "formatting"]):
        return "style"
    if any(t in tags_lower for t in ["security", "secret", "auth"]):
        return "security"
    if any(t in tags_lower for t in ["git", "commit", "version"]):
        return "git"
    if any(t in tags_lower for t in ["test", "testing"]):
        return "testing"
    return "general"
