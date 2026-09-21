"""Web Tools - 公网搜索工具。"""

from __future__ import annotations

import json
from typing import Any

DEFAULT_MAX_RESULTS = 8
MAX_MAX_RESULTS = 20
SEARCH_TIMEOUT_S = 20


def _clamp_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, parsed))


def _normalize_result(item: dict[str, Any]) -> dict[str, str]:
    return {
        "title": str(item.get("title") or "").strip(),
        "url": str(item.get("href") or item.get("url") or "").strip(),
        "snippet": str(item.get("body") or item.get("snippet") or item.get("description") or "").strip(),
    }


def web_search(inp: dict) -> str:
    query = str(inp.get("query") or "").strip()
    if not query:
        return "Error: query is required."

    max_results = _clamp_int(inp.get("max_results"), DEFAULT_MAX_RESULTS, 1, MAX_MAX_RESULTS)
    region = str(inp.get("region") or "us-en").strip() or "us-en"
    timelimit = str(inp.get("timelimit") or "").strip() or None
    backend = str(inp.get("backend") or "auto").strip() or "auto"

    try:
        from ddgs import DDGS

        raw_results = DDGS(timeout=SEARCH_TIMEOUT_S).text(
            query,
            region=region,
            safesearch="off",
            timelimit=timelimit,
            max_results=max_results,
            backend=backend,
        )
    except Exception as exc:
        return f"Error: web_search failed: {type(exc).__name__}: {exc}"

    results = [_normalize_result(item) for item in raw_results if isinstance(item, dict)]
    results = [item for item in results if item["url"] or item["snippet"]]
    if not results:
        return "No web results found."

    return json.dumps(
        {
            "query": query,
            "count": len(results),
            "results": results,
        },
        ensure_ascii=False,
        indent=2,
    )
