"""Wiki 整理 — 过期扫描 + LLM 刷新。

定期运行，标记过期条目并由 LLM 判定保留/重写/归档。
去重由写入前预检索（preflight）负责，不再在此处处理。
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agents.wiki.wiki_manager import (
    get_wiki_dir,
    list_wiki_entries,
    delete_wiki_entry,
    _git_commit,
    update_wiki_index,
    WikiEntry,
)


STALE_MONTHS = 3
MAX_REFRESH_PER_RUN = 25


def _load_settings():
    from agents.wiki.evolution.settings import get_setting
    return {
        "stale_months": get_setting("consolidate.staleAfterMonths", STALE_MONTHS),
        "max_refresh": get_setting("consolidate.maxRefreshPerRun", MAX_REFRESH_PER_RUN),
    }


# 从文件加载 side query 提示词
_PROMPTS_DIR = Path(__file__).parent.parent / "prompts" / "side_query"
CONSOLIDATE_PROMPT = (_PROMPTS_DIR / "consolidate_wiki.txt").read_text(encoding="utf-8")


async def consolidate(side_query: Any) -> dict[str, int]:
    """运行 consolidate：过期扫描 + LLM 刷新。"""
    from agents.wiki.evolution.lifecycle import disable_entry, truncate_archived_body

    cfg = _load_settings()
    stats = {"stale_marked": 0, "refreshed": 0, "archived": 0}

    stats["stale_marked"] = await _mark_stale_entries()

    stale_entries = _find_stale_entries()
    for entry in stale_entries[:cfg["max_refresh"]]:
        try:
            verdict = await _semantic_refresh(entry, side_query)
            if verdict == "rewrite":
                stats["refreshed"] += 1
            elif verdict == "archive":
                disable_entry(entry)
                truncate_archived_body(entry)
                stats["archived"] += 1
        except Exception:
            pass

    if sum(stats.values()) > 0:
        _git_commit(
            f"wiki: consolidate (stale={stats['stale_marked']}, "
            f"refresh={stats['refreshed']}, archive={stats['archived']})"
        )
        update_wiki_index()

    return stats


async def _mark_stale_entries() -> int:
    """标记 stale 条目。

    使用 last_applied（最后一次被检索注入的时间）判断是否还在使用。
    3 个月未被引用的条目标记为 stale。
    """
    entries = list_wiki_entries()
    now = datetime.now(timezone.utc)
    marked = 0

    for entry in entries:
        if entry.meta.get("stale") == "true":
            continue

        last_applied_str = entry.meta.get("last_applied", "")
        if not last_applied_str:
            last_applied_str = entry.meta.get("modified", "")
        if not last_applied_str:
            continue

        try:
            last_applied = datetime.fromisoformat(last_applied_str.replace("Z", "+00:00"))
            days_since = (now - last_applied).days
            if days_since > STALE_MONTHS * 30:
                _mark_entry_stale(entry)
                marked += 1
        except Exception:
            pass

    return marked


def _mark_entry_stale(entry: WikiEntry) -> None:
    """标记条目为 stale。"""
    wiki_dir = get_wiki_dir()
    filepath = wiki_dir / entry.rel_path
    if not filepath.exists():
        return

    from agents.memory.frontmatter import parse_frontmatter, format_frontmatter
    result = parse_frontmatter(filepath.read_text())
    result.meta["stale"] = "true"
    result.meta["stale_since"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    filepath.write_text(format_frontmatter(result.meta, result.body))


def _find_stale_entries() -> list[WikiEntry]:
    """查找 stale 条目，使用 last_applied 判断。"""
    entries = list_wiki_entries()
    now = datetime.now(timezone.utc)
    stale: list[WikiEntry] = []

    for entry in entries:
        last_applied_str = entry.meta.get("last_applied", "")
        if not last_applied_str:
            last_applied_str = entry.meta.get("modified", "")
        if not last_applied_str:
            continue
        try:
            last_applied = datetime.fromisoformat(last_applied_str.replace("Z", "+00:00"))
            days_since = (now - last_applied).days
            if days_since > STALE_MONTHS * 30:
                stale.append(entry)
        except Exception:
            pass

    stale.sort(key=lambda e: e.meta.get("last_applied", e.meta.get("modified", "")))
    return stale


async def _semantic_refresh(entry: WikiEntry, side_query: Any) -> str:
    try:
        text = await side_query(
            CONSOLIDATE_PROMPT,
            f"Wiki entry ({entry.type}): {entry.name}\n\nContent:\n{entry.content[:4000]}",
        )

        match = re.search(r"\{[\s\S]*\}", text)
        if not match:
            return "keep"

        import json
        result = json.loads(match.group(0))
        verdict = result.get("verdict", "keep")

        if verdict == "rewrite":
            new_content = result.get("new_content", "")
            if new_content:
                _rewrite_entry(entry, new_content)
            return "rewrite"
        elif verdict == "archive":
            delete_wiki_entry(entry.rel_path)
            return "archive"

        return "keep"
    except Exception:
        return "keep"


def _rewrite_entry(entry: WikiEntry, new_content: str) -> None:
    wiki_dir = get_wiki_dir()
    filepath = wiki_dir / entry.rel_path
    if not filepath.exists():
        return

    from agents.memory.frontmatter import parse_frontmatter, format_frontmatter
    result = parse_frontmatter(filepath.read_text())
    result.meta["modified"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    result.meta["last_refreshed"] = result.meta["modified"]
    filepath.write_text(format_frontmatter(result.meta, new_content))


import re
