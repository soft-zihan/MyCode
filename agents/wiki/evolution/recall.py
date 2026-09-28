"""Recall — 语义搜索 + Drop-Rung Ladder + 加权评分。

参考：projects/llm-wiki-memory/scripts/lib/recall.mjs + wiki-search.mjs
核心：embedding cosine similarity 语义搜索，fallback 到关键词匹配。
借鉴 ows：多信号加权评分，结果可解释。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from agents.observability.trace import trace_span
from agents.wiki.store import list_wiki_entries, WikiEntry


LESSON_ATOM_TYPE = "self_improvement"
KNOWLEDGE_CROSSREF_ATOM_TYPES = ["bug_root_cause", "feedback_rule"]


@dataclass
class ScoringSignal:
    """单个评分信号。"""
    signal: str
    points: float
    reason: str


DEFAULT_WEIGHTS = {
    "exact_title": 40,
    "name_match": 35,
    "semantic_similarity": 30,
    "same_module": 20,
    "same_area": 15,
    "keyword_match": 15,
    "keyword_weak": 8,
    "high_applied_count": 10,
}


def _dedup_key(entry: WikiEntry) -> str:
    return entry.rel_path


def _rank_of(entry: WikiEntry) -> float:
    return float(entry.meta.get("score", entry.meta.get("applied_count", "0")))


def _keyword_relevance(query: str, entry: WikiEntry) -> float:
    """关键词匹配 fallback。"""
    if not query:
        return 0.0
    query_words = set(re.findall(r'\w+', query.lower()))
    content = (entry.name + ' ' + entry.meta.get('description', '') + ' ' + entry.content).lower()
    content_words = set(re.findall(r'\w+', content))
    if not query_words or not content_words:
        return 0.0
    matched = query_words & content_words
    if not matched:
        return 0.0
    precision = len(matched) / len(query_words)
    recall = len(matched) / len(content_words)
    if precision + recall == 0:
        return 0.0
    return 2 * precision * recall / (precision + recall)


def score_candidate(
    query: str,
    entry: WikiEntry,
    embedding_score: float = 0.0,
    query_terms: list[str] | None = None,
    query_module: str | None = None,
    query_area: str | None = None,
) -> tuple[float, list[ScoringSignal]]:
    """加权评分，返回分数和信号列表。

    借鉴 ows 的 scoring.ts，多信号加权求和。
    """
    signals: list[ScoringSignal] = []
    query_lower = query.lower().strip()
    name_lower = entry.name.lower()

    if query_lower and query_lower == name_lower:
        signals.append(ScoringSignal(
            signal="exact_title",
            points=DEFAULT_WEIGHTS["exact_title"],
            reason=f"exact title match: {entry.name}",
        ))
    elif query_lower and query_lower in name_lower:
        signals.append(ScoringSignal(
            signal="name_match",
            points=DEFAULT_WEIGHTS["name_match"],
            reason=f"name contains query: {entry.name}",
        ))

    if embedding_score > 0.7:
        signals.append(ScoringSignal(
            signal="semantic_similarity",
            points=embedding_score * DEFAULT_WEIGHTS["semantic_similarity"],
            reason=f"semantic match: {embedding_score:.2f}",
        ))

    entry_module = entry.meta.get("project_module", "")
    if query_module and entry_module and query_module == entry_module:
        signals.append(ScoringSignal(
            signal="same_module",
            points=DEFAULT_WEIGHTS["same_module"],
            reason=f"same project_module: {entry_module}",
        ))

    entry_area = entry.meta.get("area", "")
    if query_area and entry_area and query_area == entry_area:
        signals.append(ScoringSignal(
            signal="same_area",
            points=DEFAULT_WEIGHTS["same_area"],
            reason=f"same area: {entry_area}",
        ))

    if query_terms:
        content = (entry.name + ' ' + entry.meta.get('description', '') + ' ' + entry.content).lower()
        match_count = sum(1 for t in query_terms if t.lower() in content)
        if match_count >= 2:
            signals.append(ScoringSignal(
                signal="keyword_match",
                points=DEFAULT_WEIGHTS["keyword_match"],
                reason=f"keyword match: {match_count} terms",
            ))
        elif match_count == 1:
            signals.append(ScoringSignal(
                signal="keyword_weak",
                points=DEFAULT_WEIGHTS["keyword_weak"],
                reason=f"weak keyword match: 1 term",
            ))

    applied_count = int(entry.meta.get("applied_count", "0"))
    if applied_count >= 2:
        signals.append(ScoringSignal(
            signal="high_applied_count",
            points=DEFAULT_WEIGHTS["high_applied_count"],
            reason=f"high usage: {applied_count} times",
        ))

    total = sum(s.points for s in signals)
    return total, signals


async def semantic_recall(
    query: str,
    wiki_types: list[str] | None = None,
    score_threshold: float | None = None,
    max_results: int = 10,
) -> list[tuple[WikiEntry, float]]:
    """语义搜索 — embedding cosine similarity + 加权评分。

    score_threshold 缺省取 settings recall.scoreThreshold。
    """
    from agents.wiki.evolution.settings import get_setting
    if score_threshold is None:
        score_threshold = float(get_setting("recall.scoreThreshold", 0.12))
    from agents.wiki.evolution.embedding import (
        embed_text, embed_text_for_leaf, cosine_similarity,
        ColdBudget, EmbeddingCache,
    )

    with trace_span(
        "wiki.recall.semantic",
        input=query[:200],
        metadata={"score_threshold": score_threshold},
    ) as span:
        if not query:
            if span:
                span.add_metadata(result_count=0, reason="empty_query")
            return []

        if wiki_types is None:
            wiki_types = ["knowledge", "self_improvement", "user", "reference",
                          "workflow_pattern", "plan"]

        entries: list[WikiEntry] = []
        for wt in wiki_types:
            entries.extend(list_wiki_entries(wt))

        if span:
            span.add_metadata(candidate_count=len(entries))

        if not entries:
            if span:
                span.add_metadata(result_count=0, reason="no_entries")
            return []

        cache = EmbeddingCache.get()

        from agents.wiki.evolution.settings import get_setting as _get_setting
        budget = ColdBudget(max_cold=int(_get_setting("cold.maxCold", 50)))
        query_vec = await embed_text(query)
        query_terms = re.findall(r'\w+', query.lower())

        scored: list[tuple[WikiEntry, float]] = []
        all_scores: list[tuple[str, float, float]] = []  # (rel_path, embedding_score, weighted_score)
        
        for entry in entries:
            text = embed_text_for_leaf(entry)
            cached = cache.get_embedding(text)
            if cached is not None:
                entry_vec = cached
            else:
                if not budget.draw():
                    kw_score = _keyword_relevance(query, entry)
                    if kw_score > 0:
                        scored.append((entry, kw_score * 0.5))
                        all_scores.append((entry.rel_path, 0.0, kw_score * 0.5))
                    continue
                try:
                    entry_vec = await embed_text(text)
                except Exception:
                    kw_score = _keyword_relevance(query, entry)
                    if kw_score > 0:
                        scored.append((entry, kw_score * 0.3))
                        all_scores.append((entry.rel_path, 0.0, kw_score * 0.3))
                    continue

            embedding_score = cosine_similarity(query_vec, entry_vec)
            weighted_score, _ = score_candidate(query, entry, embedding_score, query_terms)
            all_scores.append((entry.rel_path, embedding_score, weighted_score))

            if weighted_score >= score_threshold * 100:
                scored.append((entry, weighted_score / 100))

        cache.save()

        scored.sort(key=lambda x: -x[1])
        result = scored[:max_results]
        
        if span:
            top_scores = sorted(all_scores, key=lambda x: -x[2])[:5]
            metadata: dict[str, Any] = {
                "result_count": len(result),
                "top_scores": [
                    {"path": p, "emb": round(e, 3), "weighted": round(w, 1)}
                    for p, e, w in top_scores
                ],
            }
            if result:
                metadata["top_results"] = [e.rel_path for e, _ in result[:3]]
            span.add_metadata(**metadata)
        
        return result


async def hybrid_recall(
    query: str,
    wiki_types: list[str] | None = None,
    score_threshold: float | None = None,
    max_results: int = 10,
) -> list[tuple[WikiEntry, float]]:
    """混合搜索 — 语义 + 关键词 + 加权评分。

    score_threshold 缺省取 settings recall.scoreThreshold。
    """
    from agents.wiki.evolution.settings import get_setting
    if score_threshold is None:
        score_threshold = float(get_setting("recall.scoreThreshold", 0.12))
    with trace_span(
        "wiki.recall.hybrid",
        input=query[:200],
        metadata={"score_threshold": score_threshold},
    ) as span:
        semantic_results = await semantic_recall(query, wiki_types, score_threshold, max_results)

        if span:
            span.add_metadata(semantic_count=len(semantic_results))

        seen: set[str] = {e.rel_path for e, _ in semantic_results}
        keyword_results: list[tuple[WikiEntry, float]] = []

        if wiki_types is None:
            wiki_types = ["knowledge", "self_improvement", "user", "reference",
                          "workflow_pattern", "plan"]

        entries: list[WikiEntry] = []
        for wt in wiki_types:
            entries.extend(list_wiki_entries(wt))

        query_terms = re.findall(r'\w+', query.lower())

        for entry in entries:
            if entry.rel_path in seen:
                continue
            kw_score = _keyword_relevance(query, entry)
            if kw_score > 0:
                weighted_score, _ = score_candidate(query, entry, 0.0, query_terms)
                keyword_results.append((entry, weighted_score / 100))

        keyword_results.sort(key=lambda x: -x[1])

        if span:
            span.add_metadata(keyword_count=len(keyword_results))

        combined = semantic_results + keyword_results
        combined.sort(key=lambda x: -x[1])

        from agents.wiki.evolution.priority_rerank import rerank_within_bands
        combined = rerank_within_bands(combined)

        result = combined[:max_results]
        
        if span:
            metadata: dict[str, Any] = {"result_count": len(result)}
            if result:
                metadata["top_results"] = [
                    {"path": e.rel_path, "score": round(s, 3)}
                    for e, s in result[:5]
                ]
            span.add_metadata(**metadata)
        
        return result


def recall_lessons(
    query: str = "",
    project_module: str | None = None,
    area: str | None = None,
    language: str | None = None,
    task_type: str | None = None,
    error_pattern: str | None = None,
    tags: list[str] | None = None,
    include_knowledge: bool = True,
    score_threshold: float | None = None,
    max_results: int = 5,
) -> dict[str, Any]:
    """关键词 recall（同步 fallback）。"""
    from agents.wiki.evolution.settings import get_setting
    if score_threshold is None:
        score_threshold = float(get_setting("recall.scoreThreshold", 0.12))
    entries = list_wiki_entries(LESSON_ATOM_TYPE)

    results: list[tuple[WikiEntry, float]] = []
    for entry in entries:
        match = True
        if project_module and entry.meta.get("project_module") != project_module:
            match = False
        if area and entry.meta.get("area") != area:
            match = False
        if language and entry.meta.get("language") != language:
            match = False
        if task_type and entry.meta.get("task_type") != task_type:
            match = False
        if error_pattern and entry.meta.get("error_pattern") != error_pattern:
            match = False

        if match:
            relevance = _keyword_relevance(query, entry)
            if relevance > 0:
                results.append((entry, relevance))

    results.sort(key=lambda x: -x[1])
    records = [entry for entry, _ in results[:max_results]]

    return {
        "query": query,
        "lesson_hits": len(records),
        "total_records": len(records),
        "records": records,
    }
