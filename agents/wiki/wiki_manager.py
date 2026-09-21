"""Wiki 基础设施 — CRUD、索引、检索。

Wiki 是项目级持久记忆，存储在 .mycode/wiki/ 下，独立 git 仓库。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agents.core.workspace import get_workspace
from agents.core.frontmatter import parse_frontmatter, format_frontmatter

logger = logging.getLogger(__name__)

VALID_WIKI_TYPES = {
    "knowledge",
    "self_improvement",
    "feedback",
    "user",
    "reference",
    "workflow_pattern",
    "session_notes",
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
    skip_if_unchanged: bool = False,
) -> Path:
    from agents.observability.trace import trace_span
    from agents.wiki.evolution.normalise_meta import infer_facets
    from agents.wiki.evolution.wiki_commit import record_wiki_change

    with trace_span(
        "wiki.write",
        metadata={
            "wiki_type": wiki_type,
            "name": name[:100],
            "description": description[:100],
        },
    ) as span:
        wiki_dir = get_wiki_dir()
        type_dir = _ensure_type_dir(wiki_dir, wiki_type)
        if sub_dir:
            type_dir = type_dir / sub_dir
            type_dir.mkdir(parents=True, exist_ok=True)

        slug = _slugify(name)
        filename = f"{slug}.md"
        filepath = type_dir / filename
        existed = filepath.exists()

        if skip_if_unchanged and existed:
            try:
                existing = parse_frontmatter(filepath.read_text())
                existing_body = existing.body.strip()
                existing_meta = existing.meta or {}
                extra_meta_changed = any(
                    str(existing_meta.get(key, "")) != str(value)
                    for key, value in (extra_meta or {}).items()
                )
                if existing_body == str(content).strip() and not extra_meta_changed:
                    if span:
                        span.set_metadata("skipped", "unchanged")
                    return filepath
            except Exception as exc:
                logger.warning(
                    "[wiki_write] failed to compare existing entry %s: %s: %s",
                    filepath.name,
                    type(exc).__name__,
                    exc,
                )

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
        filepath.write_text(format_frontmatter(meta, content))

        try:
            import concurrent.futures
            from agents.core.snapshot_service import SnapshotService
            from agents.core.workspace import get_workspace
            snapshots_dir = Path.home() / ".mycode" / "snapshots"
            svc = SnapshotService(str(get_workspace()), str(snapshots_dir))

            def _capture():
                return asyncio.run(svc.capture(session_id="wiki", label=f"wiki:{wiki_type}/{filename}"))

            try:
                asyncio.get_running_loop()
            except RuntimeError:
                snap = _capture()
            else:
                # 同步函数被事件循环内直接调用（应经 to_thread）：在独立线程跑，避免嵌套 loop
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                    snap = pool.submit(_capture).result(timeout=30)
            meta["checkpoint_id"] = snap.id
            filepath.write_text(format_frontmatter(meta, content))
        except Exception as exc:
            logger.warning(
                "[wiki_write] snapshot capture failed for %s/%s: %s: %s",
                wiki_type,
                filename,
                type(exc).__name__,
                exc,
            )

        record_wiki_change(str(filepath.relative_to(wiki_dir)))
        _git_commit(f"wiki: {'update' if existed else 'add'} {wiki_type}/{filename}")
        update_wiki_index()
        if span:
            span.set_metadata("filepath", str(filepath.relative_to(wiki_dir)))
        return filepath


def write_workflow_pattern(
    name: str,
    symptom: str,
    root_cause: str,
    workaround: str,
    *,
    description: str = "",
    sub_dir: str = "",
    extra_meta: dict[str, str] | None = None,
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
        extra_meta={"kind": "failure", **(extra_meta or {})},
        sub_dir=sub_dir,
    )


def list_wiki_entries(wiki_type: str | None = None, include_archived: bool = False) -> list[WikiEntry]:
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
                if not include_archived and meta.get("status", "active") == "archived":
                    continue
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


def increment_usage(rel_path: str) -> None:
    """citation 命中：usage_count+1、last_used=now。

    不单独 git commit（与 increment_applied_count 一致），
    由下一次编译/整理的 commit 顺带入库，避免每次引用一个 commit。
    """
    wiki_dir = get_wiki_dir()
    filepath = wiki_dir / rel_path
    if not filepath.exists():
        return
    try:
        result = parse_frontmatter(filepath.read_text())
        count = int(result.meta.get("usage_count", "0") or 0) + 1
        result.meta["usage_count"] = str(count)
        result.meta["last_used"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        filepath.write_text(format_frontmatter(result.meta, result.body))
    except Exception as e:
        logger.warning("[wiki] increment_usage failed for %s: %s: %s", rel_path, type(e).__name__, e)


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
    except Exception as e:
        logger.warning("[wiki] increment_applied_count failed for %s: %s: %s", rel_path, type(e).__name__, e)
    # 3.3：被剪枝条目回热 → 移出剪枝名单，下次索引更新自动回归
    if _unprune_from_index(rel_path):
        update_wiki_index()


async def preflight_wiki_search(
    content: str,
    wiki_type: str,
    top_k: int = 5,
) -> list[tuple[WikiEntry, float]]:
    """写入前预检索，避免重复创建相似条目。

    Returns:
        [(entry, score), ...] 按分数降序
    """
    from agents.observability.trace import trace_span
    from agents.wiki.evolution.recall import hybrid_recall
    with trace_span(
        "wiki.preflight",
        input=content[:200],
        metadata={
            "wiki_type": wiki_type,
            "top_k": top_k,
        },
    ) as span:
        try:
            results = await hybrid_recall(
                query=content,
                wiki_types=[wiki_type],
                score_threshold=0.3,
                max_results=top_k,
            )
            if span:
                metadata: dict[str, Any] = {"found_count": len(results)}
                if results:
                    metadata["max_similarity"] = round(results[0][1], 3)
                    metadata["entries"] = [r[0].rel_path for r in results[:5]]
                span.add_metadata(**metadata)
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


def pattern_content_hash(content: str) -> str:
    """Pattern 内容哈希，用于检测 pattern 更新后 skill 需要重新编译。"""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]


def is_skill_stale(entry: WikiEntry) -> bool:
    """检查 pattern 内容是否与已编译的 skill 不一致（需要重新编译）。"""
    return entry.meta.get("compiled_content_hash") != pattern_content_hash(entry.content)


def mark_pattern_compiled(rel_path: str, content_hash: str) -> None:
    wiki_dir = get_wiki_dir()
    filepath = wiki_dir / rel_path
    if not filepath.exists():
        return
    try:
        result = parse_frontmatter(filepath.read_text())
        result.meta["compiled_to_skill"] = "true"
        result.meta["compiled_content_hash"] = content_hash
        result.meta["modified"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        filepath.write_text(format_frontmatter(result.meta, result.body))
        _git_commit(f"wiki: pattern compiled to skill {rel_path}")
    except Exception as e:
        print(f"[wiki] mark_pattern_compiled failed for {rel_path}: {type(e).__name__}: {e}")
        raise


# ── WIKI.md 索引 ──

def _get_index_path() -> Path:
    return get_wiki_dir() / "WIKI.md"


CONSOLIDATE_STATE_FILE = ".consolidate_state.json"


def _load_index_state() -> dict:
    path = get_wiki_dir() / CONSOLIDATE_STATE_FILE
    if path.exists():
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("[wiki] consolidate state parse error: %s: %s", type(e).__name__, e)
    return {}


def _save_index_state(state: dict) -> None:
    (get_wiki_dir() / CONSOLIDATE_STATE_FILE).write_text(
        json.dumps(state, ensure_ascii=False, indent=2)
    )


def _unprune_from_index(rel_path: str) -> bool:
    """条目回热：从剪枝名单移除。返回名单是否有变化。"""
    state = _load_index_state()
    pruned = state.get("pruned_from_index", [])
    if rel_path not in pruned:
        return False
    pruned.remove(rel_path)
    state["pruned_from_index"] = pruned
    _save_index_state(state)
    return True


def _coldness_key(entry: WikiEntry) -> tuple:
    """越冷越靠前：新鲜度 asc → usage_count asc → applied_count asc。"""
    freshness = (
        entry.meta.get("last_used")
        or entry.meta.get("last_applied")
        or entry.meta.get("modified")
        or ""
    )
    return (
        freshness,
        int(entry.meta.get("usage_count", "0") or 0),
        int(entry.meta.get("applied_count", "0") or 0),
    )


def _build_index_lines(entries: list[WikiEntry], with_desc: set[str]) -> list[str]:
    sections: dict[str, list[str]] = {}
    for e in entries:
        section = e.type.replace("_", " ").title()
        if section not in sections:
            sections[section] = []
        pending = " [待确认]" if e.meta.get("pending_confirm") == "true" else ""
        compiled = " [已编译]" if e.meta.get("compiled_to_skill") == "true" else ""
        desc = e.meta.get("description", "") if e.rel_path in with_desc else ""
        line = f"- {e.name}{pending}{compiled} → {e.rel_path}"
        if desc:
            line = f"- {desc}{pending}{compiled} → {e.rel_path}"
        sections[section].append(line)

    lines = ["# Wiki Index", ""]
    section_order = [
        "Knowledge", "Self Improvement", "Feedback", "User", "Reference",
        "Workflow Pattern",
    ]
    for section in section_order:
        if section in sections:
            lines.append(f"## {section}")
            lines.extend(sections[section])
            lines.append("")
    return lines


def _over_budget(lines: list[str]) -> bool:
    return len(lines) > MAX_INDEX_LINES or len("\n".join(lines).encode()) > MAX_INDEX_BYTES


def update_wiki_index() -> None:
    """生成 WIKI.md 索引（3.3：超预算闭环剪枝，不再只靠静默截断）。

    剪枝两级：最冷条目先去 description，仍超预算则整行移出索引
    （条目本身不动，仍可被召回；回热时经 _unprune_from_index 自动回归）。
    """
    state = _load_index_state()
    pruned: set[str] = set(state.get("pruned_from_index", []))
    entries = [e for e in list_wiki_entries() if e.rel_path not in pruned]

    with_desc = {e.rel_path for e in entries if e.meta.get("description")}
    lines = _build_index_lines(entries, with_desc)

    while _over_budget(lines) and entries:
        coldest = min(entries, key=_coldness_key)
        if coldest.rel_path in with_desc:
            with_desc.discard(coldest.rel_path)
        else:
            entries.remove(coldest)
            pruned.add(coldest.rel_path)
        lines = _build_index_lines(entries, with_desc)

    _get_index_path().write_text("\n".join(lines))

    new_pruned = sorted(pruned)
    if new_pruned != sorted(state.get("pruned_from_index", [])):
        state["pruned_from_index"] = new_pruned
        _save_index_state(state)


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


# ── Wiki 召回（side query） ──

# 从文件加载 side query 提示词
_PROMPTS_DIR = Path(__file__).parent.parent / "prompts" / "side_query"
SELECT_WIKI_PROMPT = (_PROMPTS_DIR / "select_wiki.txt").read_text(encoding="utf-8")


def _format_wiki_manifest(entries: list[WikiEntry]) -> str:
    lines = []
    for e in entries:
        tag = f"[{e.type}] "
        desc = e.meta.get("description", "")
        lines.append(f"- {tag}{e.rel_path}: {desc}")
    return "\n".join(lines)


async def _try_compile_skill(pattern_rel_path: str, side_query: Any) -> None:
    """尝试将高频 workflow_pattern 编译为 skill。"""
    from agents.observability.trace import trace_span
    with trace_span(
        "skill.compile",
        metadata={"pattern_rel_path": pattern_rel_path},
    ) as span:
        try:
            from agents.wiki.wiki_compiler import compile_to_skill
            skill_path = await compile_to_skill(pattern_rel_path, side_query)
            if skill_path:
                if span:
                    span.add_metadata(compiled=True, skill_path=str(skill_path))
                print(f"[skill_compile] compiled {pattern_rel_path} -> {skill_path}")
            else:
                if span:
                    span.add_metadata(compiled=False, reason="skipped")
                print(f"[skill_compile] skipped {pattern_rel_path} (applied_count < 2 or skill already up-to-date)")
        except Exception as e:
            if span:
                span.record_error(e)
                span.add_metadata(compiled=False, reason="error")
            print(f"[skill_compile] error: {type(e).__name__}: {e}")


async def select_relevant_wiki_entries(
    query: str,
    side_query: Any,
    already_surfaced: set[str],
) -> list[WikiEntry]:
    import time
    from agents.observability.trace import trace_span
    t0 = time.time()
    
    with trace_span(
        "wiki.recall",
        input=query[:200],
        metadata={"already_surfaced_count": len(already_surfaced)},
    ) as span:
        entries = list_wiki_entries()
        if not entries:
            if span:
                span.add_metadata(recalled_count=0, reason="no_entries")
            print(f"[wiki_select] no entries, took {time.time()-t0:.2f}s")
            return []

        candidates = [e for e in entries if e.rel_path not in already_surfaced]
        if not candidates:
            if span:
                span.add_metadata(recalled_count=0, reason="all_already_surfaced")
            print(f"[wiki_select] all entries already surfaced, took {time.time()-t0:.2f}s")
            return []

        try:
            from agents.wiki.evolution.recall import hybrid_recall
            t1 = time.time()
            scored = await hybrid_recall(query, max_results=10)
            recall_time = time.time() - t1
            # B5：hybrid_recall 全量召回，主路径必须过滤冷却中条目再取 top5
            scored = [(e, sc) for e, sc in scored if e.rel_path not in already_surfaced]
            if scored:
                result: list[WikiEntry] = []
                for entry, score in scored[:5]:
                    increment_applied_count(entry.rel_path)
                    # 重新读取 entry 以获取更新后的 applied_count
                    updated_entry = read_wiki_entry(entry.rel_path)
                    if updated_entry:
                        entry = updated_entry
                    # 检查是否需要编译为 skill（未编译，或 pattern 内容已变更）
                    if entry.type == "workflow_pattern":
                        applied_count = int(entry.meta.get("applied_count", "0"))
                        if applied_count >= 2 and is_skill_stale(entry):
                            # 异步编译 skill
                            asyncio.create_task(
                                _try_compile_skill(entry.rel_path, side_query)
                            )
                            print(f"[skill_compile] triggered for {entry.rel_path} (applied_count={applied_count})")
                    result.append(entry)
                if span:
                    span.add_metadata(
                        recalled_count=len(result),
                        recall_time_s=round(recall_time, 3),
                        entries=[e.rel_path for e in result],
                    )
                print(f"[wiki_select] hybrid_recall found {len(scored)} entries in {recall_time:.2f}s, returning {len(result)}")
                return result
            else:
                if span:
                    span.add_metadata(recalled_count=0, recall_time_s=round(recall_time, 3))
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
                span.add_metadata(
                    recalled_count=len(result),
                    method="side_query",
                    entries=[e.rel_path for e in result],
                )
            return result
        except Exception:
            return []


def format_wiki_for_injection(entries: list[WikiEntry]) -> str:
    parts = []
    for e in entries:
        parts.append(
            f"<system-reminder>\nWiki ({e.type}): {e.rel_path}\n\n{e.content}\n"
            f"[若本次回复实际使用了该条目，在回复末尾输出 <wiki-citation>{e.rel_path}</wiki-citation>]"
            f"\n</system-reminder>"
        )
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
    except Exception as exc:
        logger.warning("[wiki_git] commit failed: %s: %s", type(exc).__name__, exc)


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
    except Exception as exc:
        logger.warning("[wiki_git] init failed: %s: %s", type(exc).__name__, exc)


# ── System prompt 段 ──

def build_wiki_prompt_section() -> str:
    index = load_wiki_index()
    wiki_dir = str(get_wiki_dir())

    return f"""# Wiki System

You have a persistent, file-based wiki system at `{wiki_dir}`.

## Wiki Categories

### 1. Session Notes (session_notes/)
- Each session has one stable note file: `session_{{session_id}}.md`
- Session folding merges previous notes with newly folded context and updates the file in place
- Searchable via `search_history` tool to find past session context
- Deleted when the corresponding session is deleted
- Use `list_session_notes` and `read_session_notes` tools to browse and read details

### 2. Long-term Memory (knowledge/, workflow_pattern/, self_improvement/, feedback/, user/, reference/)
- Persistent knowledge recalled automatically based on current task
- workflow_pattern entries can be compiled into executable Skills when high-frequency
- No manual action needed - recall is automatic

### 3. Plans (plans/)
- Plan artifacts with their own lifecycle
- Can be converted to/from wiki entries when appropriate

## Wiki Recall
Wiki entries are automatically recalled based on your current task. You don't need to manually search.

## When to Save (use the `remember` tool — NEVER write wiki files directly)
- User explicitly says "remember X / always X / never X again" → call remember() IMMEDIATELY in the current turn, do not wait for session end
- User corrects your behavior → remember(wiki_type=feedback) in the current turn; content MUST contain the Rule, Why, and How to apply
- When you learn a user preference → remember(wiki_type=user)
- Project facts/decisions/conventions worth persisting → remember(wiki_type=knowledge)
- External links/docs → remember(wiki_type=reference)
- Reusable troubleshooting pattern → remember(wiki_type=workflow_pattern, symptom/root_cause/workaround)
- remember() returns created/merged/appended — when merged or appended, tell the user which existing entry was updated
- Type boundary: feedback = user-initiated correction/confirmation; self_improvement = Agent's own mistakes (extraction pipeline only, not writable via remember)

## What NOT to Save
- Code patterns you can read from the codebase
- Git history
- Ephemeral task details
{chr(10) + "## Current Wiki Index" + chr(10) + index if index else chr(10) + "(No wiki entries yet.)"}"""
