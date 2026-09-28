"""Lifecycle 管理 — archive/truncate 替代硬删除。

参考：projects/llm-wiki-memory/scripts/lib/wiki-lifecycle.mjs
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from agents.wiki.store import get_wiki_dir, WikiEntry
from agents.core.frontmatter import parse_frontmatter, format_frontmatter


def disable_entry(entry: WikiEntry) -> None:
    """软删除 — 标记 status: archived。"""
    wiki_dir = get_wiki_dir()
    filepath = wiki_dir / entry.rel_path
    if not filepath.exists():
        return

    result = parse_frontmatter(filepath.read_text())
    result.meta["status"] = "archived"
    result.meta["archived_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    filepath.write_text(format_frontmatter(result.meta, result.body))


def enable_entry(entry: WikiEntry) -> None:
    """恢复 active。"""
    wiki_dir = get_wiki_dir()
    filepath = wiki_dir / entry.rel_path
    if not filepath.exists():
        return

    result = parse_frontmatter(filepath.read_text())
    result.meta["status"] = "active"
    if "archived_at" in result.meta:
        del result.meta["archived_at"]
    filepath.write_text(format_frontmatter(result.meta, result.body))


def truncate_archived_body(entry: WikiEntry, keep_chars: int = 200) -> None:
    """截断 archived leaf 的 body。"""
    wiki_dir = get_wiki_dir()
    filepath = wiki_dir / entry.rel_path
    if not filepath.exists():
        return

    result = parse_frontmatter(filepath.read_text())
    if result.meta.get("status") != "archived":
        return

    if len(result.body) > keep_chars:
        result.body = result.body[:keep_chars] + "\n\n[... truncated ...]"
        result.meta["consolidate_truncated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        filepath.write_text(format_frontmatter(result.meta, result.body))


def is_active(entry: WikiEntry) -> bool:
    """是否 active。"""
    return entry.meta.get("status", "active") != "archived"
