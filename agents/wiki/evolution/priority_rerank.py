"""Priority-band rerank — 在 cosine band 内按 priority 重排。

参考：projects/llm-wiki-memory/scripts/lib/wiki-search-rank.mjs
"""

from __future__ import annotations

from typing import Any

from agents.wiki.wiki_manager import WikiEntry


PRIORITY_ORDER = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}


def priority_rank(priority: str) -> int:
    """获取 priority 排名（越小越优先）。"""
    return PRIORITY_ORDER.get(priority, 99)


def rerank_within_bands(
    items: list[tuple[WikiEntry, float]],
    band_width: float | None = None,
) -> list[tuple[WikiEntry, float]]:
    """在 cosine band 内按 priority 重排。

    band_width 缺省取 settings recall.priorityBand。
    """
    if not items:
        return []

    if band_width is None:
        from agents.wiki.evolution.settings import get_setting
        band_width = float(get_setting("recall.priorityBand", 0.05))

    items.sort(key=lambda x: -x[1])

    result: list[tuple[WikiEntry, float]] = []
    band_start = 0

    for i, (entry, score) in enumerate(items):
        if score < items[band_start][1] - band_width:
            band_items = items[band_start:i]
            band_items.sort(key=lambda x: priority_rank(x[0].meta.get("priority", "P2")))
            result.extend(band_items)
            band_start = i

    band_items = items[band_start:]
    band_items.sort(key=lambda x: priority_rank(x[0].meta.get("priority", "P2")))
    result.extend(band_items)

    return result
