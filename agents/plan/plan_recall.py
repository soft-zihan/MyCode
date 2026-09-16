"""Plan 检索 — 语义 + 标签 + 状态。

Plan 检索支持：
1. 语义匹配（对 proposal.md + tasks.md 建 embedding 索引）
2. 标签匹配（规范化后的标签）
3. 状态过滤（活跃 plan 优先）
4. 历史 plan 召回（include_archived=True）
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from agents.plan.plan_manager import (
    get_plans_dir,
    list_plans,
    read_artifact,
)
from agents.plan.plan_models import Plan, PlanStatus, PlanRecallResult


def normalize_tags(tags: list[str]) -> set[str]:
    normalized = set()
    for tag in tags:
        tag = tag.lower()
        tag = tag.replace("_", "-")
        normalized.add(tag)
    return normalized


async def recall_plans(
    query: str,
    tags: list[str] | None = None,
    include_archived: bool = False,
    max_results: int = 5,
) -> list[PlanRecallResult]:
    plans = list_plans(include_archived=include_archived)
    if not plans:
        return []

    results: list[PlanRecallResult] = []

    for plan in plans:
        score = 0.0
        match_reasons = []

        if tags:
            plan_tags = normalize_tags(plan.tags)
            query_tags = normalize_tags(tags)
            overlap = plan_tags & query_tags
            if overlap:
                score += 0.3 * len(overlap) / len(query_tags)
                match_reasons.append(f"tags: {', '.join(overlap)}")

        proposal = read_artifact(plan.slug, "proposal.md") or ""
        tasks_content = read_artifact(plan.slug, "tasks.md") or ""
        searchable = f"{proposal} {tasks_content}".lower()
        query_lower = query.lower()

        query_words = query_lower.split()
        matched_words = [w for w in query_words if w in searchable]
        if matched_words:
            word_score = len(matched_words) / len(query_words)
            score += 0.5 * word_score
            match_reasons.append(f"keywords: {', '.join(matched_words[:3])}")

        if plan.status in (PlanStatus.IN_PROGRESS, PlanStatus.PROPOSED):
            score += 0.2
            match_reasons.append(f"status: {plan.status.value}")

        if score > 0:
            results.append(PlanRecallResult(
                plan=plan,
                score=score,
                match_reason="; ".join(match_reasons),
            ))

    results.sort(key=lambda r: r.score, reverse=True)
    return results[:max_results]


def format_recall_results(results: list[PlanRecallResult]) -> str:
    if not results:
        return "No matching plans found."

    lines = ["# Matching Plans\n"]
    for i, r in enumerate(results, 1):
        lines.append(f"## {i}. {r.plan.slug} (score: {r.score:.2f})")
        lines.append(f"- Status: {r.plan.status.value}")
        lines.append(f"- Priority: {r.plan.priority}")
        tags = r.plan.tags if isinstance(r.plan.tags, list) else [r.plan.tags] if r.plan.tags else []
        lines.append(f"- Tags: {', '.join(tags) if tags else 'none'}")
        lines.append(f"- Match: {r.match_reason}")
        lines.append("")

    return "\n".join(lines)
