from __future__ import annotations

import json
import re
from typing import Any, Awaitable, Callable

from agents._utils import (
    ratio as _ratio,
    pct as _pct,
)

SideQuery = Callable[[str, str], Awaitable[str]]

_RE_URL = re.compile(r"https?://\S+")
_RE_MARKDOWN_LINK = re.compile(r"\[[^\]]+\]\((https?://[^)]+)\)")
_RE_SOURCE_LABEL = re.compile(r"(?im)^\s*(source|sources|reference|references|来源|参考)\s*[:：]")
_RE_JSON_PREFIX = re.compile(r"^\s*[\{\[]")
_RE_CONCLUSION = re.compile(r"(?i)\b(tl;dr|answer|conclusion|bottom line)\b|结论|先说结论")
_RE_PARAGRAPH_LIMIT = re.compile(r"(不超过|少于|最多|within|less than|at most)\s*(\d+)\s*(段|paragraph)")


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "").strip().lower())


def _parse_json_object(text: str) -> dict[str, Any]:
    raw = str(text or "").strip()
    if not raw:
        return {}
    try:
        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else {}
    except Exception:
        pass
    start = raw.find("{")
    end = raw.rfind("}")
    if start >= 0 and end > start:
        try:
            obj = json.loads(raw[start : end + 1])
            return obj if isinstance(obj, dict) else {}
        except Exception:
            return {}
    return {}


def _compile_eval_rules(skill: dict[str, Any], *, include_llm_rules: bool = False) -> list[dict[str, Any]]:
    corpus = "\n".join(
        [
            str(skill.get("name") or ""),
            str(skill.get("description") or ""),
            str(skill.get("when_to_use") or ""),
            str(skill.get("instructions") or ""),
            " ".join(str(tag) for tag in skill.get("tags", []) if str(tag).strip()),
        ]
    )
    low = _normalize_text(corpus)
    rules: list[dict[str, Any]] = [
        {
            "rule_id": "response_nonempty",
            "label": "Non-empty response",
            "kind": "programmatic",
            "hard": True,
            "params": {"mode": "nonempty"},
            "provenance": {"source": "baseline"},
        }
    ]

    skill_requirement = _skill_alignment_requirement(skill)
    if include_llm_rules and skill_requirement:
        rules.append(
            {
                "rule_id": "skill_instruction_alignment",
                "label": "Follows skill instructions",
                "kind": "llm_binary",
                "hard": False,
                "params": {
                    "mode": "requirement",
                    "requirement_text": skill_requirement,
                },
                "provenance": {"source": "skill_text"},
            }
        )

    if any(key in low for key in ("引用来源", "标注来源", "注明来源", "cite sources", "with sources", "provide sources", "source-backed")):
        rules.append(
            {
                "rule_id": "must_cite_sources",
                "label": "Cite sources",
                "kind": "programmatic",
                "hard": True,
                "params": {"mode": "mentions_sources"},
                "provenance": {"source": "skill_text"},
            }
        )

    para_limit = _paragraph_limit(corpus)
    if para_limit:
        rules.append(
            {
                "rule_id": "paragraph_limit",
                "label": f"At most {para_limit} paragraphs",
                "kind": "programmatic",
                "hard": True,
                "params": {"mode": "max_paragraphs", "max_paragraphs": para_limit},
                "provenance": {"source": "skill_text"},
            }
        )

    if any(key in low for key in ("先给结论", "结论在前", "先说结论", "answer first", "lead with the conclusion", "bottom line first")):
        rules.append(
            {
                "rule_id": "lead_with_conclusion",
                "label": "Lead with conclusion",
                "kind": "programmatic",
                "hard": False,
                "params": {"mode": "lead_with_conclusion"},
                "provenance": {"source": "skill_text"},
            }
        )

    if "json" in low or "结构化输出" in low:
        rules.append(
            {
                "rule_id": "json_parseable",
                "label": "Valid JSON output",
                "kind": "programmatic",
                "hard": True,
                "params": {"mode": "json_parseable"},
                "provenance": {"source": "skill_text"},
            }
        )

    if "markdown table" in low or "表格" in low:
        rules.append(
            {
                "rule_id": "markdown_table",
                "label": "Markdown table present",
                "kind": "programmatic",
                "hard": False,
                "params": {"mode": "markdown_table"},
                "provenance": {"source": "skill_text"},
            }
        )

    if include_llm_rules and any(
        key in low
        for key in (
            "不要幻觉",
            "不要编造",
            "不确定就说",
            "don't hallucinate",
            "do not hallucinate",
            "avoid hallucination",
            "if unsure",
        )
    ):
        rules.append(
            {
                "rule_id": "no_unfounded_claims",
                "label": "Avoid unfounded claims",
                "kind": "llm_binary",
                "hard": True,
                "params": {
                    "mode": "requirement",
                    "requirement_text": "Avoid unfounded claims and state uncertainty when needed.",
                },
                "provenance": {"source": "skill_text"},
            }
        )
    elif any(key in low for key in ("不要幻觉", "不要编造", "不确定就说", "do not hallucinate", "avoid hallucination", "if unsure")):
        rules.append(
            {
                "rule_id": "uncertainty_marked",
                "label": "Mark uncertainty",
                "kind": "programmatic",
                "hard": False,
                "params": {"mode": "uncertainty_marked"},
                "provenance": {"source": "skill_text"},
            }
        )

    return rules[:8]


def _skill_alignment_requirement(skill: dict[str, Any], *, max_chars: int = 3200) -> str:
    parts: list[str] = []
    name = str(skill.get("name") or "").strip()
    description = str(skill.get("description") or "").strip()
    when_to_use = str(skill.get("when_to_use") or "").strip()
    instructions = str(skill.get("instructions") or "").strip()
    tags = [str(tag).strip() for tag in skill.get("tags", []) if str(tag).strip()]

    if name:
        parts.append(f"Skill name: {name}")
    if description:
        parts.append(f"Description: {description}")
    if when_to_use:
        parts.append(f"When to use: {when_to_use}")
    if tags:
        parts.append("Tags: " + ", ".join(tags))
    if instructions:
        parts.append("Instructions:\n" + instructions)

    body = "\n\n".join(parts).strip()
    if not body:
        return ""
    if len(body) > max_chars:
        body = body[: max_chars - 80].rstrip() + "\n\n[Truncated to fit judge context.]"
    return (
        "Evaluate whether the assistant response follows this Skill's observable requirements. "
        "Judge only the response quality for the given user message; do not require hidden tool calls or information that is not visible in the response. "
        "Return false when the response clearly violates important instructions, misses the requested style/format, or ignores the Skill's intended behavior.\n\n"
        + body
    )


def _paragraph_limit(text: str) -> int:
    for match in _RE_PARAGRAPH_LIMIT.finditer(str(text or "")):
        try:
            return max(1, int(match.group(2)))
        except Exception:
            continue
    low = _normalize_text(text)
    if "少于 3 段" in low or "不超过 3 段" in low or "3 paragraphs" in low:
        return 3
    return 0


def _check_json_parseable(stripped: str) -> tuple[bool, dict[str, Any]]:
    if not _RE_JSON_PREFIX.search(stripped):
        return False, {"reason": "missing_json_prefix"}
    try:
        json.loads(stripped)
        return True, {}
    except Exception as exc:
        return False, {"reason": "json_parse_failed", "error": str(exc)}


def _evaluate_rule(rule: dict[str, Any], response_text: str) -> dict[str, Any]:
    params = rule.get("params") if isinstance(rule.get("params"), dict) else {}
    mode = str(params.get("mode") or "").strip()
    text = str(response_text or "")
    stripped = text.strip()
    passed = False
    details: dict[str, Any] = {}

    if mode == "nonempty":
        passed = bool(stripped)
        details = {"length": len(stripped)}
    elif mode == "json_parseable":
        passed, details = _check_json_parseable(stripped)
    elif mode == "mentions_sources":
        passed = bool(_RE_URL.search(text) or _RE_MARKDOWN_LINK.search(text) or _RE_SOURCE_LABEL.search(text))
        details = {"has_url": bool(_RE_URL.search(text))}
    elif mode == "lead_with_conclusion":
        first_block = stripped.split("\n\n", 1)[0].strip()
        passed = bool(_RE_CONCLUSION.search(first_block)) or len(first_block) <= 100
        details = {"first_block": first_block[:160]}
    elif mode == "max_paragraphs":
        paras = [part.strip() for part in re.split(r"\n\s*\n", stripped) if part.strip()]
        limit = max(1, int(params.get("max_paragraphs", 3) or 3))
        passed = len(paras) <= limit
        details = {"paragraph_count": len(paras), "limit": limit}
    elif mode == "markdown_table":
        lines = [line for line in text.splitlines() if "|" in line]
        passed = len(lines) >= 2 and any("---" in line for line in lines)
        details = {"table_line_count": len(lines)}
    elif mode == "uncertainty_marked":
        uncertainty_terms = ("不确定", "无法确认", "需要验证", "uncertain", "not sure", "cannot verify")
        fabrication_terms = ("可能", "假设", "if", "assuming", "needs verification")
        passed = any(term in stripped.lower() for term in uncertainty_terms + fabrication_terms)
        details = {"heuristic": "uncertainty_marker"}
    else:
        details = {"error": f"unsupported programmatic mode: {mode}"}

    return {
        "rule_id": rule.get("rule_id", ""),
        "label": rule.get("label", ""),
        "hard": bool(rule.get("hard")),
        "passed": bool(passed),
        "details": details,
    }


async def _evaluate_rule_async(
    rule: dict[str, Any],
    response_text: str,
    *,
    sample: dict[str, Any],
    skill_name: str,
    side_query: SideQuery | None,
) -> dict[str, Any]:
    kind = str(rule.get("kind") or "programmatic").strip()
    if kind == "programmatic":
        return _evaluate_rule(rule, response_text)

    if kind != "llm_binary":
        return {
            "rule_id": rule.get("rule_id", ""),
            "label": rule.get("label", ""),
            "hard": bool(rule.get("hard")),
            "passed": False,
            "details": {"error": f"unsupported rule kind: {kind}"},
        }

    requirement = str((rule.get("params") or {}).get("requirement_text") or rule.get("description") or "").strip()
    if not requirement:
        return {
            "rule_id": rule.get("rule_id", ""),
            "label": rule.get("label", ""),
            "hard": bool(rule.get("hard")),
            "passed": False,
            "details": {"error": "missing_requirement_text"},
        }
    if side_query is None:
        return {
            "rule_id": rule.get("rule_id", ""),
            "label": rule.get("label", ""),
            "hard": bool(rule.get("hard")),
            "passed": False,
            "skipped": True,
            "details": {"reason": "judge_llm_not_configured"},
        }

    system = (
        "You are a strict binary evaluator for skill replay results.\n"
        "Output ONLY strict JSON parseable by json.loads.\n"
        'Schema: {"pass": true|false, "reason": "short reason"}\n'
        "Judge only against the requirement provided.\n"
        "Do not write analysis, chain-of-thought, markdown, or any text outside the JSON object.\n"
        "Prefer false if the requirement is not clearly satisfied.\n"
    )
    payload = {
        "requirement": requirement,
        "skill_name": skill_name,
        "latest_user_message": sample.get("latest_user", ""),
        "response": str(response_text or ""),
    }
    raw_judge_response = ""
    try:
        raw_judge_response = await side_query(system, json.dumps(payload, ensure_ascii=False))
        parsed = _parse_json_object(raw_judge_response)
    except Exception as exc:
        parsed = {"pass": False, "reason": f"judge failed: {exc}"}
    reason = str(parsed.get("reason") or "").strip()
    if not reason:
        if not str(raw_judge_response or "").strip():
            reason = "judge returned an empty response"
        elif not parsed:
            reason = "judge returned non-JSON response"
        else:
            reason = "judge returned no reason"
    details = {"reason": reason[:500]}
    if raw_judge_response and not parsed:
        details["raw_response_preview"] = str(raw_judge_response).strip()[:500]
    return {
        "rule_id": rule.get("rule_id", ""),
        "label": rule.get("label", ""),
        "hard": bool(rule.get("hard")),
        "passed": bool(parsed.get("pass", False)),
        "details": details,
    }


_RULE_ADDITIONS: dict[str, str] = {
    "must_cite_sources": "Always cite concrete sources or links for factual claims.",
    "lead_with_conclusion": "Open with a direct conclusion or answer before elaboration.",
    "json_parseable": "Output valid JSON only, with no markdown fences or extra commentary.",
    "markdown_table": "Include a markdown table when presenting structured comparisons or plans.",
    "no_unfounded_claims": "Do not invent facts; if evidence is missing, state uncertainty explicitly.",
    "skill_instruction_alignment": "Satisfy the Skill's observable requirements before adding extra clarifications or commentary.",
}


def _additions_for_failure(failure: dict[str, Any], rule: dict[str, Any]) -> list[str]:
    rid = str(failure.get("rule_id") or "").strip()
    details = failure.get("details") if isinstance(failure.get("details"), dict) else {}
    reason = str(details.get("reason") or details.get("error") or "").strip()

    additions: list[str] = []
    if rid in _RULE_ADDITIONS:
        additions.append(_RULE_ADDITIONS[rid])
    elif rid == "paragraph_limit":
        limit = int((rule.get("params") or {}).get("max_paragraphs", 3) or 3)
        additions.append(f"Keep the final answer within {limit} short paragraphs.")
    else:
        requirement = str((rule.get("params") or {}).get("requirement_text") or "").strip()
        if requirement:
            additions.append(f"Requirement to preserve: {requirement[:500]}")
    if reason:
        additions.append(f"Address prior evaluation failure: {reason[:500]}")
    return additions


def _skill_status(
    *,
    replay_count: int,
    promotion_test_count: int,
    retrieved: int,
    relevant: int,
    used: int,
    pruned: bool,
    rule_summary: dict[str, Any],
    min_replay_samples: int,
    min_promotion_tests: int,
    min_retrieved: int,
    min_used_rate: float,
    min_relevance_rate: float,
    min_rule_pass_rate: float,
) -> tuple[str, list[str]]:
    reasons: list[str] = []
    if pruned:
        return "pruned", ["skill has been archived by usage pruning"]
    if replay_count <= 0 and retrieved <= 0:
        return "unobserved", ["no online replay or usage signal yet"]
    if replay_count < min_replay_samples:
        reasons.append(f"only {replay_count} replay sample(s)")
    if promotion_test_count < min_promotion_tests:
        reasons.append(f"only {promotion_test_count} promotion-test sample(s)")
    if retrieved < min_retrieved:
        reasons.append(f"only {retrieved} retrieval judgment(s)")
    if reasons:
        return "incubating", reasons

    relevance_rate = _ratio(relevant, retrieved)
    used_rate = _ratio(used, retrieved)
    pass_rate = float(rule_summary.get("pass_rate", 0.0) or 0.0)
    test_hard_failures = int(rule_summary.get("promotion_test_hard_failures", 0) or 0)
    hard_failures = int(rule_summary.get("hard_failures", 0) or 0)

    if test_hard_failures > 0:
        reasons.append(f"{test_hard_failures} promotion-test hard rule failure(s)")
    elif hard_failures > 0:
        reasons.append(f"{hard_failures} hard rule failure(s)")
    if pass_rate < min_rule_pass_rate:
        reasons.append(f"low replay rule pass rate {_pct(pass_rate)}")
    if relevance_rate < min_relevance_rate:
        reasons.append(f"low relevance rate {_pct(relevance_rate)}")
    if used_rate < min_used_rate:
        reasons.append(f"low used rate {_pct(used_rate)}")
    if reasons:
        return "watch", reasons
    return "healthy", ["passes replay rules and usage gates"]
