"""Wiki 基础设施 — CRUD、索引、检索。

Wiki 是项目级持久记忆，存储在 .mycode/wiki/ 下，独立 git 仓库。
"""

from __future__ import annotations

import asyncio
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agents.core.workspace import get_workspace
from agents.memory.frontmatter import parse_frontmatter, format_frontmatter


VALID_WIKI_TYPES = {
    "knowledge",
    "self_improvement",
    "user",
    "reference",
    "workflow_pattern",
    "session_notes",
    "project",  # Project documentation and test scripts
}

MAX_INDEX_LINES = 200
MAX_INDEX_BYTES = 25000


def get_wiki_dir() -> Path:
    d = get_workspace() / ".mycode" / "wiki"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "_", text.lower())
    s = s.strip("_")
    return s[:40] or hashlib.md5(text.encode()).hexdigest()[:8]


import hashlib


class WikiEntry:
    __slots__ = ("name", "type", "filename", "rel_path", "content", "meta")

    def __init__(self, name: str, type: str, filename: str, rel_path: str,
                 content: str, meta: dict | None = None):
        self.name = name
        self.type = type
        self.filename = filename
        self.rel_path = rel_path
        self.content = content
        self.meta = meta or {}


def _ensure_type_dir(wiki_dir: Path, wiki_type: str) -> Path:
    d = wiki_dir / wiki_type
    d.mkdir(parents=True, exist_ok=True)
    return d


def write_wiki_entry(
    wiki_type: str,
    name: str,
    content: str,
    *,
    description: str = "",
    extra_meta: dict[str, str] | None = None,
    sub_dir: str = "",
) -> Path:
    from agents.observability.tracer import tracer
    from agents.wiki.evolution.normalise_meta import infer_facets
    from agents.wiki.evolution.wiki_commit import record_wiki_change

    with tracer.span("wiki.write", {
        "langfuse.observation.type": "chain",
        "mycode.wiki.wiki_type": wiki_type,
        "mycode.wiki.name": name[:100],
        "mycode.wiki.description": description[:100],
    }) as span:
        wiki_dir = get_wiki_dir()
        type_dir = _ensure_type_dir(wiki_dir, wiki_type)
        if sub_dir:
            type_dir = type_dir / sub_dir
            type_dir.mkdir(parents=True, exist_ok=True)

        slug = _slugify(name)
        filename = f"{slug}.md"
        filepath = type_dir / filename

        raw_meta: dict[str, str] = {
            "name": name,
            "type": wiki_type,
            "description": description,
            "modified": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "applied_count": "0",
        }
        if extra_meta:
            raw_meta.update(extra_meta)

        meta = infer_facets(raw_meta, wiki_type)

        # session_notes 类型每次写新文件（带时间戳）
        if wiki_type == "session_notes":
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            filename = f"session_{session_id}_{timestamp}.md"
            filepath = type_dir / filename
        else:
            slug = _slugify(name)
            filename = f"{slug}.md"
            filepath = type_dir / filename

        filepath.write_text(format_frontmatter(meta, content))

        try:
            from agents.core.snapshot_service import SnapshotService
            from agents.core.workspace import get_workspace
            snapshots_dir = Path.home() / ".mycode" / "snapshots"
            svc = SnapshotService(str(get_workspace()), str(snapshots_dir))
            try:
                loop = asyncio.get_running_loop()
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor() as pool:
                    snap = loop.run_in_executor(
                        pool,
                        lambda: asyncio.run(svc.capture(session_id="wiki", label=f"wiki:{wiki_type}/{filename}"))
                    ).result()
            except RuntimeError:
                snap = asyncio.run(svc.capture(session_id="wiki", label=f"wiki:{wiki_type}/{filename}"))
            meta["checkpoint_id"] = snap.id
            filepath.write_text(format_frontmatter(meta, content))
        except Exception:
            pass

        record_wiki_change(str(filepath.relative_to(wiki_dir)))
        _git_commit(f"wiki: add {wiki_type}/{filename}")
        update_wiki_index()
        if span:
            span.set_attribute("mycode.wiki.filepath", str(filepath.relative_to(wiki_dir)))
        return filepath


def write_workflow_pattern(
    name: str,
    symptom: str,
    root_cause: str,
    workaround: str,
    *,
    description: str = "",
    sub_dir: str = "",
) -> Path:
    content = f"""## Symptom
{symptom}

## Root cause
{root_cause}

## Workaround
{workaround}"""
    return write_wiki_entry(
        "workflow_pattern", name, content,
        description=description or symptom[:80],
        extra_meta={"kind": "failure"},
        sub_dir=sub_dir,
    )


def list_wiki_entries(wiki_type: str | None = None) -> list[WikiEntry]:
    wiki_dir = get_wiki_dir()
    entries: list[WikiEntry] = []

    types_to_scan = [wiki_type] if wiki_type else list(VALID_WIKI_TYPES)
    for wt in types_to_scan:
        type_dir = wiki_dir / wt
        if not type_dir.exists():
            continue
        for f in type_dir.rglob("*.md"):
            try:
                result = parse_frontmatter(f.read_text())
                meta = result.meta
                if not meta.get("name") or not meta.get("type"):
                    continue
                t = meta["type"] if meta["type"] in VALID_WIKI_TYPES else wt
                rel_path = str(f.relative_to(wiki_dir))
                entries.append(WikiEntry(
                    name=meta["name"],
                    type=t,
                    filename=f.name,
                    rel_path=rel_path,
                    content=result.body,
                    meta=meta,
                ))
            except Exception:
                pass

    entries.sort(key=lambda e: e.meta.get("modified", ""), reverse=True)
    return entries


def read_wiki_entry(rel_path: str) -> WikiEntry | None:
    wiki_dir = get_wiki_dir()
    filepath = wiki_dir / rel_path
    if not filepath.exists():
        return None
    try:
        result = parse_frontmatter(filepath.read_text())
        meta = result.meta
        return WikiEntry(
            name=meta.get("name", ""),
            type=meta.get("type", ""),
            filename=filepath.name,
            rel_path=rel_path,
            content=result.body,
            meta=meta,
        )
    except Exception:
        return None


def delete_wiki_entry(rel_path: str) -> bool:
    wiki_dir = get_wiki_dir()
    filepath = wiki_dir / rel_path
    if not filepath.exists():
        return False
    filepath.unlink()
    _git_commit(f"wiki: delete {rel_path}")
    update_wiki_index()
    return True


def confirm_wiki_entry(rel_path: str) -> None:
    wiki_dir = get_wiki_dir()
    filepath = wiki_dir / rel_path
    if not filepath.exists():
        return
    try:
        result = parse_frontmatter(filepath.read_text())
        result.meta["pending_confirm"] = "false"
        result.meta["modified"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        filepath.write_text(format_frontmatter(result.meta, result.body))
        _git_commit(f"wiki: confirm {rel_path}")
        update_wiki_index()
    except Exception:
        pass


def list_pending_confirm_entries() -> list[WikiEntry]:
    entries = list_wiki_entries()
    return [e for e in entries if e.meta.get("pending_confirm") == "true"]


def increment_applied_count(rel_path: str) -> None:
    wiki_dir = get_wiki_dir()
    filepath = wiki_dir / rel_path
    if not filepath.exists():
        return
    try:
        result = parse_frontmatter(filepath.read_text())
        count = int(result.meta.get("applied_count", "0")) + 1
        result.meta["applied_count"] = str(count)
        result.meta["last_applied"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        filepath.write_text(format_frontmatter(result.meta, result.body))
    except Exception:
        pass


async def preflight_wiki_search(
    content: str,
    wiki_type: str,
    top_k: int = 5,
) -> list[tuple[WikiEntry, float]]:
    """写入前预检索，避免重复创建相似条目。

    Returns:
        [(entry, score), ...] 按分数降序
    """
    from agents.observability.tracer import tracer
    from agents.wiki.evolution.recall import hybrid_recall
    with tracer.span("wiki.preflight", {
        "langfuse.observation.type": "chain",
        "mycode.wiki.query": content[:200],
        "mycode.wiki.wiki_type": wiki_type,
        "mycode.wiki.top_k": top_k,
    }) as span:
        try:
            results = await hybrid_recall(
                query=content,
                wiki_types=[wiki_type],
                score_threshold=0.3,
                max_results=top_k,
            )
            if span:
                span.set_attribute("mycode.wiki.found_count", len(results))
                if results:
                    span.set_attribute("mycode.wiki.max_similarity", round(results[0][1], 3))
                    span.set_attribute("mycode.wiki.entries", [r[0].rel_path for r in results[:5]])
            return results
        except Exception as e:
            if span:
                span.record_error(e)
            return []


def merge_wiki_entry(
    existing: WikiEntry,
    new_content: str,
    *,
    mode: str = "append",
) -> Path:
    """合并内容到现有条目。

    Args:
        existing: 现有条目
        new_content: 新内容
        mode: "append" 追加 | "replace" 替换

    Returns:
        更新后的文件路径
    """
    wiki_dir = get_wiki_dir()
    filepath = wiki_dir / existing.rel_path

    try:
        result = parse_frontmatter(filepath.read_text())
        if mode == "append":
            combined = result.body.rstrip() + "\n\n" + new_content.strip()
        else:
            combined = new_content.strip()

        result.meta["modified"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        filepath.write_text(format_frontmatter(result.meta, combined))
        _git_commit(f"wiki: update {existing.rel_path}")
        update_wiki_index()
        return filepath
    except Exception:
        raise


def mark_pattern_compiled(rel_path: str) -> None:
    wiki_dir = get_wiki_dir()
    filepath = wiki_dir / rel_path
    if not filepath.exists():
        return
    try:
        result = parse_frontmatter(filepath.read_text())
        result.meta["compiled_to_skill"] = "true"
        result.meta["modified"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        filepath.write_text(format_frontmatter(result.meta, result.body))
        _git_commit(f"wiki: pattern compiled to skill {rel_path}")
    except Exception:
        pass


# ── WIKI.md 索引 ──

def _get_index_path() -> Path:
    return get_wiki_dir() / "WIKI.md"


def update_wiki_index() -> None:
    entries = list_wiki_entries()

    sections: dict[str, list[str]] = {}
    for e in entries:
        section = e.type.replace("_", " ").title()
        if section not in sections:
            sections[section] = []
        desc = e.meta.get("description", "")
        pending = " [待确认]" if e.meta.get("pending_confirm") == "true" else ""
        compiled = " [已编译]" if e.meta.get("compiled_to_skill") == "true" else ""
        line = f"- {e.name}{pending}{compiled} → {e.rel_path}"
        if desc:
            line = f"- {desc}{pending}{compiled} → {e.rel_path}"
        sections[section].append(line)

    lines = ["# Wiki Index", ""]
    section_order = [
        "Knowledge", "Self Improvement", "User", "Reference",
        "Workflow Pattern",
    ]
    for section in section_order:
        if section in sections:
            lines.append(f"## {section}")
            lines.extend(sections[section])
            lines.append("")

    _get_index_path().write_text("\n".join(lines))


def load_wiki_index() -> str:
    index_path = _get_index_path()
    if not index_path.exists():
        return ""
    content = index_path.read_text()
    lines = content.split("\n")
    if len(lines) > MAX_INDEX_LINES:
        content = "\n".join(lines[:MAX_INDEX_LINES]) + "\n\n[... truncated, too many wiki entries ...]"
    encoded = content.encode()
    if len(encoded) > MAX_INDEX_BYTES:
        cut = encoded[:MAX_INDEX_BYTES]
        content = cut.decode(errors="ignore") + "\n\n[... truncated, index too large ...]"
    return content


def list_project_docs() -> list[str]:
    """List project documentation files."""
    project_dir = get_wiki_dir() / "project"
    if not project_dir.exists():
        return []
    
    docs = []
    for f in project_dir.rglob("*.md"):
        rel_path = f.relative_to(project_dir)
        docs.append(str(rel_path))
    for f in project_dir.rglob("*.py"):
        if "test" in f.name.lower():
            rel_path = f.relative_to(project_dir)
            docs.append(str(rel_path))
    
    return sorted(docs)[:20]  # Limit to 20 entries


# ── Wiki 召回（side query） ──

SELECT_WIKI_PROMPT = """You are selecting wiki entries that will be useful to an AI coding assistant as it processes a user's query. You will be given the user's query and a list of available wiki entries with their filenames and descriptions.

Return a JSON object with a "selected_entries" array of rel_paths for the entries that will clearly be useful (up to 5). Only include entries that you are certain will be helpful based on their name and description.
- If you are unsure if an entry will be useful, do not include it.
- If no entries would clearly be useful, return an empty array.

IMPORTANT: Do NOT answer the user's query. Do NOT explain. Respond with ONLY the JSON object, nothing else. Example: {"selected_entries": ["knowledge/architecture/api-design.md"]}"""


def _format_wiki_manifest(entries: list[WikiEntry]) -> str:
    lines = []
    for e in entries:
        tag = f"[{e.type}] "
        desc = e.meta.get("description", "")
        lines.append(f"- {tag}{e.rel_path}: {desc}")
    return "\n".join(lines)


async def _try_compile_skill(pattern_rel_path: str, side_query: Any) -> None:
    """尝试将高频 workflow_pattern 编译为 skill。"""
    from agents.observability.tracer import tracer
    with tracer.span("skill.compile", {
        "langfuse.observation.type": "chain",
        "mycode.skill.pattern_rel_path": pattern_rel_path,
    }) as span:
        try:
            from agents.wiki.wiki_compiler import compile_to_skill
            skill_path = await compile_to_skill(pattern_rel_path, side_query)
            if skill_path:
                if span:
                    span.set_attribute("mycode.skill.compiled", True)
                    span.set_attribute("mycode.skill.skill_path", str(skill_path))
                print(f"[skill_compile] compiled {pattern_rel_path} -> {skill_path}")
            else:
                if span:
                    span.set_attribute("mycode.skill.compiled", False)
                    span.set_attribute("mycode.skill.reason", "skipped")
                print(f"[skill_compile] skipped {pattern_rel_path} (applied_count < 2 or already compiled)")
        except Exception as e:
            if span:
                span.record_error(e)
                span.set_attribute("mycode.skill.compiled", False)
                span.set_attribute("mycode.skill.reason", "error")
            print(f"[skill_compile] error: {type(e).__name__}: {e}")


async def select_relevant_wiki_entries(
    query: str,
    side_query: Any,
    already_surfaced: set[str],
) -> list[WikiEntry]:
    import time
    from agents.observability.tracer import tracer
    t0 = time.time()
    
    with tracer.span("wiki.recall", {
        "langfuse.observation.type": "chain",
        "mycode.wiki.query": query[:200],
        "mycode.wiki.already_surfaced_count": len(already_surfaced),
    }) as span:
        entries = list_wiki_entries()
        if not entries:
            if span:
                span.set_attribute("mycode.wiki.recalled_count", 0)
                span.set_attribute("mycode.wiki.reason", "no_entries")
            print(f"[wiki_select] no entries, took {time.time()-t0:.2f}s")
            return []

        candidates = [e for e in entries if e.rel_path not in already_surfaced]
        if not candidates:
            if span:
                span.set_attribute("mycode.wiki.recalled_count", 0)
                span.set_attribute("mycode.wiki.reason", "all_already_surfaced")
            print(f"[wiki_select] all entries already surfaced, took {time.time()-t0:.2f}s")
            return []

        try:
            from agents.wiki.evolution.recall import hybrid_recall
            t1 = time.time()
            scored = await hybrid_recall(query, max_results=10)
            recall_time = time.time() - t1
            if scored:
                result: list[WikiEntry] = []
                for entry, score in scored[:5]:
                    increment_applied_count(entry.rel_path)
                    # 重新读取 entry 以获取更新后的 applied_count
                    updated_entry = read_wiki_entry(entry.rel_path)
                    if updated_entry:
                        entry = updated_entry
                    # 检查是否需要编译为 skill
                    if entry.type == "workflow_pattern":
                        applied_count = int(entry.meta.get("applied_count", "0"))
                        if applied_count >= 2 and not entry.meta.get("compiled_to_skill"):
                            # 异步编译 skill
                            asyncio.create_task(
                                _try_compile_skill(entry.rel_path, side_query)
                            )
                            print(f"[skill_compile] triggered for {entry.rel_path} (applied_count={applied_count})")
                    result.append(entry)
                if span:
                    span.set_attribute("mycode.wiki.recalled_count", len(result))
                    span.set_attribute("mycode.wiki.recall_time_s", round(recall_time, 3))
                    span.set_attribute("mycode.wiki.entries", [e.rel_path for e in result])
                print(f"[wiki_select] hybrid_recall found {len(scored)} entries in {recall_time:.2f}s, returning {len(result)}")
                return result
            else:
                if span:
                    span.set_attribute("mycode.wiki.recalled_count", 0)
                    span.set_attribute("mycode.wiki.recall_time_s", round(recall_time, 3))
                print(f"[wiki_select] hybrid_recall found 0 entries in {recall_time:.2f}s")
        except Exception as e:
            if span:
                span.record_error(e)
            print(f"[wiki_select] hybrid_recall error: {type(e).__name__}: {e}")

        manifest = _format_wiki_manifest(candidates)
        try:
            import json as json_mod
            text = await side_query(
                SELECT_WIKI_PROMPT,
                f"Query: {query}\n\nAvailable wiki entries:\n{manifest}",
            )
            match = re.search(r"\{[\s\S]*\}", text)
            selected_paths: list[str] = []
            if match:
                try:
                    parsed = json_mod.loads(match.group(0))
                    selected_paths = parsed.get("selected_entries", [])
                except Exception:
                    selected_paths = []
            if not selected_paths:
                selected_paths = [e.rel_path for e in candidates if e.rel_path in text]

            by_path = {e.rel_path: e for e in candidates}
            selected = [by_path[p] for p in selected_paths if p in by_path][:5]

            result = []
            for e in selected:
                increment_applied_count(e.rel_path)
                result.append(e)
            if span:
                span.set_attribute("mycode.wiki.recalled_count", len(result))
                span.set_attribute("mycode.wiki.method", "side_query")
                span.set_attribute("mycode.wiki.entries", [e.rel_path for e in result])
            return result
        except Exception:
            return []


def format_wiki_for_injection(entries: list[WikiEntry]) -> str:
    parts = []
    for e in entries:
        parts.append(f"<system-reminder>\nWiki ({e.type}): {e.rel_path}\n\n{e.content}\n</system-reminder>")
    return "\n\n".join(parts)


# ── Git ──

def _git_commit(message: str) -> None:
    wiki_dir = get_wiki_dir()
    try:
        subprocess.run(
            ["git", "add", "-A"],
            cwd=wiki_dir, capture_output=True, timeout=10,
        )
        subprocess.run(
            ["git", "commit", "-m", message, "--allow-empty"],
            cwd=wiki_dir, capture_output=True, timeout=10,
        )
    except Exception:
        pass


def init_wiki_git() -> None:
    wiki_dir = get_wiki_dir()
    git_dir = wiki_dir / ".git"
    if git_dir.exists():
        return
    try:
        subprocess.run(["git", "init"], cwd=wiki_dir, capture_output=True, timeout=10)
        subprocess.run(["git", "add", "-A"], cwd=wiki_dir, capture_output=True, timeout=10)
        subprocess.run(
            ["git", "commit", "-m", "wiki: init", "--allow-empty"],
            cwd=wiki_dir, capture_output=True, timeout=10,
        )
    except Exception:
        pass


# ── System prompt 段 ──

def build_wiki_prompt_section() -> str:
    index = load_wiki_index()
    wiki_dir = str(get_wiki_dir())
    project_docs = list_project_docs()

    project_section = ""
    if project_docs:
        project_section = f"""
## Project Documentation
Available project docs and test scripts at `{wiki_dir}/project/`:
{chr(10).join(f"- {p}" for p in project_docs[:10])}
Read these for project-specific context and testing guidance."""

    return f"""# Wiki System

You have a persistent, file-based wiki system at `{wiki_dir}`.

## Wiki Categories

### 1. Session Notes (session_notes/)
- Temporary notes from each session, written incrementally
- Searchable via `search_history` tool to find past session context
- Deleted when the corresponding session is deleted
- Use `list_session_notes` and `read_session_notes` tools to browse and read details

### 2. Long-term Memory (knowledge/, workflow_pattern/, self_improvement/, user/, reference/)
- Persistent knowledge recalled automatically based on current task
- workflow_pattern entries can be compiled into executable Skills when high-frequency
- No manual action needed - recall is automatic

### 3. Project Documentation (project/)
- Project-specific docs and test scripts
- Can be updated via project git repo or by you after completing work
- When updating, ensure version alignment with project state
{project_section}

### 4. Plans (plans/)
- Plan artifacts with their own lifecycle
- Can be converted to/from wiki entries when appropriate

## Wiki Recall
Wiki entries are automatically recalled based on your current task. You don't need to manually search.

## When to Save
- When the user corrects you or gives feedback → self_improvement
- When you discover a debugging lesson → self_improvement or knowledge
- When you learn a user preference → user
- When you find a reusable pattern → workflow_pattern
- When you complete significant work → update project docs if needed

## What NOT to Save
- Code patterns you can read from the codebase
- Git history
- Ephemeral task details
{chr(10) + "## Current Wiki Index" + chr(10) + index if index else chr(10) + "(No wiki entries yet.)"}"""
