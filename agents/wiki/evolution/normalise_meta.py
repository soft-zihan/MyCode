"""normaliseMeta — metadata 标准化 + Facet 推断。

参考：projects/llm-wiki-memory/scripts/lib/wiki-identity.mjs + facets-core.mjs
"""

from __future__ import annotations

from typing import Any


VALID_ATOM_TYPES = {
    "knowledge": [
        "bug-root-cause", "feedback-rule", "api-convention",
        "architecture-decision", "code-pattern", "config-fact",
    ],
    "self_improvement": [
        "self-improvement-lesson",
    ],
    "user": [
        "user-preference", "user-fact",
    ],
    "reference": [
        "external-reference",
    ],
    "knowledge_pattern": [
        "reusable-pattern",
    ],
    "workflow_pattern": [
        "reusable-workflow",
    ],
    "plan": [
        "task-plan",
    ],
}


def normalise_meta(meta: dict[str, Any], wiki_type: str | None = None) -> dict[str, Any]:
    """标准化 metadata。

    参考：wiki-identity.mjs:normaliseMeta
    """
    result = dict(meta)

    if wiki_type and "type" not in result:
        result["type"] = wiki_type

    actual_type = result.get("type", wiki_type or "")

    if "atom_type" not in result and actual_type in VALID_ATOM_TYPES:
        defaults = VALID_ATOM_TYPES[actual_type]
        if defaults:
            result["atom_type"] = defaults[0]

    if "project_module" not in result:
        result["project_module"] = "unscoped"

    if "area" not in result:
        result["area"] = infer_area(result, actual_type)

    if "task_type" not in result and actual_type == "self_improvement":
        result["task_type"] = "unknown"

    if "status" not in result:
        result["status"] = "active"

    if "priority" not in result:
        result["priority"] = default_priority(result.get("atom_type", ""))

    for key in ["tags", "subject", "stale", "supersedes_id",
                "consolidated_at", "last_refreshed_at", "quality", "full"]:
        if key in result and not result[key]:
            del result[key]

    return result


def infer_area(meta: dict[str, Any], wiki_type: str) -> str:
    """推断 area facet。

    参考：facets-core.mjs:inferFacets
    """
    if meta.get("area"):
        return meta["area"]

    if meta.get("project_module") and meta["project_module"] != "unscoped":
        return meta["project_module"]

    tags = meta.get("tags", "")
    if isinstance(tags, str) and tags:
        tag_list = [t.strip() for t in tags.split(",")]
        for tag in tag_list:
            if tag in KNOWN_AREAS:
                return tag

    return "unscoped"


KNOWN_AREAS = {
    "frontend", "backend", "api", "database", "devops",
    "testing", "security", "performance", "architecture",
    "ui", "ux", "mobile", "web", "desktop",
}


def default_priority(atom_type: str) -> str:
    """默认 priority。

    参考：datasets.mjs:priorityForAtomType
    """
    high_priority = ["bug-root-cause", "feedback-rule"]
    if atom_type in high_priority:
        return "P1"
    return "P2"


def infer_facets(meta: dict[str, Any], wiki_type: str) -> dict[str, Any]:
    """写入时推断 placement facets。

    参考：facets-core.mjs:inferFacets
    """
    result = normalise_meta(meta, wiki_type)

    if wiki_type == "self_improvement":
        if not result.get("task_type"):
            result["task_type"] = "unknown"
        if not result.get("error_pattern"):
            result["error_pattern"] = "unknown"

    if wiki_type == "knowledge":
        if not result.get("atom_type") or result["atom_type"] not in VALID_ATOM_TYPES.get("knowledge", []):
            result["atom_type"] = "code-pattern"

    return result
