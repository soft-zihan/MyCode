"""Rule file operations — create, evolve, and manage .mycode/RULES.md."""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

from agents._utils import utc_now as _utc_now


RULES_FILE_NAME = "RULES.md"
RULES_PROVENANCE_LOG = "rules_provenance.jsonl"


def get_rules_file() -> Path:
    return Path.cwd() / ".mycode" / RULES_FILE_NAME


def get_rules_evolution_dir() -> Path:
    return Path.cwd() / ".mycode" / "rule-evolution"


def _today() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime())


def _append_jsonl(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def _preview(value: object, limit: int = 500) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "...[truncated]"


def load_rules() -> str:
    """Load current RULES.md content."""
    rules_file = get_rules_file()
    if not rules_file.is_file():
        return ""
    try:
        return rules_file.read_text(encoding="utf-8")
    except Exception:
        return ""


def create_rule(
    *,
    rule_text: str,
    category: str = "general",
    evidence: str = "",
    actor: str = "online",
) -> dict[str, Any]:
    """Add a new rule to RULES.md."""
    rule_text = str(rule_text or "").strip()
    if not rule_text:
        return {"ok": False, "error": "rule_text is required"}

    rules_file = get_rules_file()
    rules_file.parent.mkdir(parents=True, exist_ok=True)

    entry = f"- [{category}] {rule_text}"
    if evidence:
        entry += f"  \n  _Evidence: {evidence}_"

    if rules_file.is_file():
        content = rules_file.read_text(encoding="utf-8")
        if "\n## Evolution Notes" in content:
            evo_idx = content.rfind("\n## Evolution Notes")
            new_content = content[:evo_idx] + entry + "\n\n" + content[evo_idx:]
        else:
            new_content = content.rstrip() + "\n" + entry + "\n"
    else:
        new_content = f"# Project Rules\n\n{entry}\n"

    rules_file.write_text(new_content, encoding="utf-8")

    event = {
        "event": "create_rule",
        "time": _utc_now(),
        "actor": actor,
        "category": category,
        "rule_text": _preview(rule_text, 1200),
        "evidence": _preview(evidence, 1200),
        "file": str(rules_file),
    }
    _append_jsonl(get_rules_evolution_dir() / RULES_PROVENANCE_LOG, event)

    return {"ok": True, **event}


def evolve_rule(
    *,
    old_rule_text: str,
    new_rule_text: str,
    category: str = "general",
    evidence: str = "",
    rationale: str = "",
    actor: str = "online",
) -> dict[str, Any]:
    """Update an existing rule in RULES.md."""
    old_rule_text = str(old_rule_text or "").strip()
    new_rule_text = str(new_rule_text or "").strip()

    if not old_rule_text or not new_rule_text:
        return {"ok": False, "error": "old_rule_text and new_rule_text are required"}

    rules_file = get_rules_file()
    if not rules_file.is_file():
        return {"ok": False, "error": "RULES.md does not exist"}

    content = rules_file.read_text(encoding="utf-8")

    old_pattern = re.escape(f"[{category}] {old_rule_text}")
    old_pattern_alt = re.escape(old_rule_text)

    new_entry = f"[{category}] {new_rule_text}"

    found = False
    if re.search(old_pattern, content):
        content = re.sub(old_pattern + r"[^\n]*", new_entry, content, count=1)
        found = True
    elif re.search(old_pattern_alt, content):
        content = re.sub(old_pattern_alt + r"[^\n]*", new_entry, content, count=1)
        found = True

    if not found:
        return {"ok": False, "error": f"Rule not found: {old_rule_text[:100]}"}

    evo_note = f"- {_today()}: Updated rule. Reason: {rationale or evidence}"
    if "\n## Evolution Notes" in content:
        content = content.replace("\n## Evolution Notes", f"\n## Evolution Notes\n\n{evo_note}")
    else:
        content = content.rstrip() + f"\n\n## Evolution Notes\n\n{evo_note}\n"

    rules_file.write_text(content, encoding="utf-8")

    event = {
        "event": "evolve_rule",
        "time": _utc_now(),
        "actor": actor,
        "category": category,
        "old_rule_text": _preview(old_rule_text, 1200),
        "new_rule_text": _preview(new_rule_text, 1200),
        "evidence": _preview(evidence, 1200),
        "rationale": _preview(rationale, 1200),
        "file": str(rules_file),
    }
    _append_jsonl(get_rules_evolution_dir() / RULES_PROVENANCE_LOG, event)

    return {"ok": True, **event}


def delete_rule(
    *,
    rule_text: str,
    category: str = "general",
    rationale: str = "",
    actor: str = "online",
) -> dict[str, Any]:
    """Remove a rule from RULES.md."""
    rule_text = str(rule_text or "").strip()
    if not rule_text:
        return {"ok": False, "error": "rule_text is required"}

    rules_file = get_rules_file()
    if not rules_file.is_file():
        return {"ok": False, "error": "RULES.md does not exist"}

    content = rules_file.read_text(encoding="utf-8")

    old_pattern = re.escape(f"[{category}] {rule_text}")
    old_pattern_alt = re.escape(rule_text)

    found = False
    if re.search(old_pattern, content):
        content = re.sub(r"[ \t]*" + old_pattern + r"[^\n]*\n?", "", content, count=1)
        found = True
    elif re.search(old_pattern_alt, content):
        content = re.sub(r"[ \t]*" + old_pattern_alt + r"[^\n]*\n?", "", content, count=1)
        found = True

    if not found:
        return {"ok": False, "error": f"Rule not found: {rule_text[:100]}"}

    rules_file.write_text(content, encoding="utf-8")

    event = {
        "event": "delete_rule",
        "time": _utc_now(),
        "actor": actor,
        "category": category,
        "rule_text": _preview(rule_text, 1200),
        "rationale": _preview(rationale, 1200),
        "file": str(rules_file),
    }
    _append_jsonl(get_rules_evolution_dir() / RULES_PROVENANCE_LOG, event)

    return {"ok": True, **event}
