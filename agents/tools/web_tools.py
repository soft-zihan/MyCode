"""Web Tools - 公网搜索工具。

双后端：DuckDuckGo（ddgs）为主，Exa 官方远程 MCP（免 key）为备。
backend=auto（默认）时主后端失败或零结果自动切换，错误透明记录不静默。
"""

from __future__ import annotations

import json
import logging
import urllib.request
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_MAX_RESULTS = 8
MAX_MAX_RESULTS = 20
SEARCH_TIMEOUT_S = 20

EXA_MCP_URL = "https://mcp.exa.ai/mcp"
# mcp.exa.ai 拦截 urllib 默认 UA，必须自定义
EXA_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) mycode/1.0"


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


def _search_ddgs(query: str, max_results: int, region: str, timelimit: str | None) -> list[dict[str, str]]:
    from ddgs import DDGS

    raw_results = DDGS(timeout=SEARCH_TIMEOUT_S).text(
        query,
        region=region,
        safesearch="off",
        timelimit=timelimit,
        max_results=max_results,
        backend="auto",
    )
    results = [_normalize_result(item) for item in raw_results if isinstance(item, dict)]
    return [item for item in results if item["url"] or item["snippet"]]


def _parse_exa_text(text: str) -> list[dict[str, str]]:
    """解析 Exa web_search_exa 的文本结果块。

    格式（每块）：
        Title: ...
        URL: ...
        Published: ... / Author: ...
        Highlights:
        <摘要行，含 '...' 省略标记>
    """
    results: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    in_highlights = False
    highlight_lines: list[str] = []

    def flush() -> None:
        nonlocal current, highlight_lines
        if current is not None:
            snippet = " ".join(l for l in highlight_lines if l and l != "...")
            current["snippet"] = snippet[:600]
            if current["url"] or current["snippet"]:
                results.append(current)
        current = None
        highlight_lines = []

    for line in text.splitlines():
        if line.startswith("Title: "):
            flush()
            current = {"title": line[7:].strip(), "url": "", "snippet": ""}
            in_highlights = False
        elif current is None:
            continue
        elif line.startswith("URL: "):
            current["url"] = line[5:].strip()
        elif line.startswith("Highlights:"):
            in_highlights = True
        elif in_highlights and not line.startswith(("Published: ", "Author: ")):
            highlight_lines.append(line.strip())
    flush()
    return results


def _exa_rpc(payload: dict, session: str | None = None) -> tuple[str | None, str]:
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "User-Agent": EXA_UA,
    }
    if session:
        headers["Mcp-Session-Id"] = session
    req = urllib.request.Request(EXA_MCP_URL, data=json.dumps(payload).encode(), headers=headers)
    resp = urllib.request.urlopen(req, timeout=SEARCH_TIMEOUT_S)
    return resp.headers.get("Mcp-Session-Id"), resp.read().decode()


def _search_exa(query: str, max_results: int) -> list[dict[str, str]]:
    session, _ = _exa_rpc({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26", "capabilities": {},
            "clientInfo": {"name": "mycode", "version": "1.0"},
        },
    })
    _exa_rpc({"jsonrpc": "2.0", "method": "notifications/initialized"}, session)
    _, body = _exa_rpc({
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {
            "name": "web_search_exa",
            "arguments": {"query": query, "numResults": max_results},
        },
    }, session)

    results: list[dict[str, str]] = []
    for line in body.splitlines():
        if not line.startswith("data: "):
            continue
        data = json.loads(line[6:])
        if data.get("error"):
            raise RuntimeError(f"exa MCP error: {data['error']}")
        for block in data.get("result", {}).get("content", []):
            if block.get("type") == "text":
                results.extend(_parse_exa_text(block.get("text") or ""))
    return results


def web_search(inp: dict) -> str:
    query = str(inp.get("query") or "").strip()
    if not query:
        return "Error: query is required."

    max_results = _clamp_int(inp.get("max_results"), DEFAULT_MAX_RESULTS, 1, MAX_MAX_RESULTS)
    region = str(inp.get("region") or "us-en").strip() or "us-en"
    timelimit = str(inp.get("timelimit") or "").strip() or None
    backend = str(inp.get("backend") or "auto").strip().lower() or "auto"

    if backend not in ("auto", "ddgs", "exa"):
        return f"Error: unknown backend '{backend}' (expected auto|ddgs|exa)."
    attempts = ["ddgs", "exa"] if backend == "auto" else [backend]

    errors: list[str] = []
    for name in attempts:
        try:
            if name == "ddgs":
                results = _search_ddgs(query, max_results, region, timelimit)
            else:
                results = _search_exa(query, max_results)
        except Exception as exc:
            errors.append(f"{name}: {type(exc).__name__}: {exc}")
            logger.warning("[web_search] backend %s failed: %s: %s", name, type(exc).__name__, exc)
            continue
        if not results:
            errors.append(f"{name}: no results")
            logger.warning("[web_search] backend %s returned no results", name)
            continue
        return json.dumps(
            {"query": query, "backend": name, "count": len(results), "results": results},
            ensure_ascii=False,
            indent=2,
        )

    return "Error: web_search failed: " + "; ".join(errors)
