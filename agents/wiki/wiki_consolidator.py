"""Wiki 整理 — git-diff 驱动（Phase 3，学 Codex knowledge consolidation）。

触发：编译成功后或 remember 写入后调用 maybe_schedule_consolidate()，
门槛 = 距上次整理 ≥24h 且 wiki repo 自上次整理 commit 起 ≥5 个条目变更。
异步执行不阻塞；模块级 task 引用防重入（单进程）。

流程：
1. git diff --name-status {last_commit}..HEAD → A/M/D 清单（仅条目目录）
2. 选材（预算 top 100）：A/M 全文 + 关联条目（preflight top3）+ stale 清单
3. side LLM 单次调用（consolidate_v2.txt）→ JSON 动作列表
4. 程序执行动作（不信 LLM 直接改文件）：merge/supersede/archive/rewrite
5. 校验 → update_wiki_index → git commit → 推进 .consolidate_state.json

状态失败不推进，下次触发重试（幂等：diff 为空直接成功推进）。
归档一律软删（disable + truncate），保留追溯链。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agents.wiki.store import (
    CONSOLIDATE_STATE_FILE,
    get_wiki_dir,
    list_wiki_entries,
    read_wiki_entry,
    merge_wiki_entry,
    _git_commit,
    update_wiki_index,
    WikiEntry,
)
from agents.wiki.wiki_manager import preflight_wiki_search

logger = logging.getLogger(__name__)

CONSOLIDATE_COOLDOWN_HOURS = 24
MIN_CHANGED_ENTRIES = 5
SELECTION_BUDGET = 100
ENTRY_CLIP_CHARS = 4000
RELATED_LOOKUPS = 20
STALE_CAP = 30

ENTRY_DIRS = {
    "knowledge", "self_improvement", "feedback",
    "user", "reference", "workflow_pattern",
}

VALID_ACTIONS = {"keep", "merge", "supersede", "archive", "rewrite"}

_PROMPTS_DIR = Path(__file__).parent.parent / "prompts" / "side_query"
CONSOLIDATE_V2_PROMPT = (_PROMPTS_DIR / "consolidate_v2.txt").read_text(encoding="utf-8")

# 防重入：单进程模块级 task 引用
_consolidate_task: asyncio.Task | None = None


# ── 状态 ──

def _state_path() -> Path:
    return get_wiki_dir() / CONSOLIDATE_STATE_FILE


def load_consolidate_state() -> dict[str, Any]:
    path = _state_path()
    if path.exists():
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("[consolidate] state parse error, resetting: %s: %s", type(e).__name__, e)
    return {}


def save_consolidate_state(state: dict[str, Any]) -> None:
    _state_path().write_text(json.dumps(state, ensure_ascii=False, indent=2))


# ── git ──

def _git(args: list[str]) -> tuple[int, str]:
    result = subprocess.run(
        ["git", *args], cwd=get_wiki_dir(),
        capture_output=True, text=True, timeout=15,
    )
    return result.returncode, result.stdout.strip()


def _head_commit() -> str | None:
    code, out = _git(["rev-parse", "HEAD"])
    return out if code == 0 and out else None


def _is_entry_path(path: str) -> bool:
    return path.split("/")[0] in ENTRY_DIRS and path.endswith(".md")


def changed_entry_files(base: str | None, head: str) -> list[tuple[str, str]]:
    """base..head 的条目变更清单 [(status, path)]。base 无效时视为全量 A。"""
    if base:
        code, out = _git(["diff", "--name-status", f"{base}..{head}"])
        if code == 0:
            changes = []
            for line in out.splitlines():
                parts = line.split("\t")
                if len(parts) >= 2:
                    status, path = parts[0][:1], parts[-1]
                    if _is_entry_path(path):
                        changes.append((status, path))
            return changes
    # base 缺失/无效：全量视为新增
    code, out = _git(["ls-files"])
    if code != 0:
        return []
    return [("A", p) for p in out.splitlines() if _is_entry_path(p)]


# ── 门槛与调度 ──

def should_consolidate() -> bool:
    """24h 冷却 + ≥5 条目变更。"""
    state = load_consolidate_state()
    last_at = state.get("last_consolidate_at", "")
    if last_at:
        try:
            last = datetime.fromisoformat(last_at)
            age_h = (datetime.now(timezone.utc) - last).total_seconds() / 3600
            if age_h < CONSOLIDATE_COOLDOWN_HOURS:
                return False
        except ValueError:
            pass

    head = _head_commit()
    if not head:
        return False
    changes = changed_entry_files(state.get("last_consolidate_commit"), head)
    return len(changes) >= MIN_CHANGED_ENTRIES


def maybe_schedule_consolidate(side_query: Any) -> bool:
    """触发点（编译成功/remember 写入后）调用。返回是否调度了整理 task。"""
    global _consolidate_task
    if side_query is None:
        return False
    if _consolidate_task is not None and not _consolidate_task.done():
        return False
    if not should_consolidate():
        return False
    _consolidate_task = asyncio.create_task(run_consolidate(side_query))
    return True


# ── stale（吸收旧 consolidate 的标记逻辑）──

def _entry_freshness_key(entry: WikiEntry) -> str:
    """4.4：last_used → last_applied → modified。"""
    return (
        entry.meta.get("last_used")
        or entry.meta.get("last_applied")
        or entry.meta.get("modified")
        or ""
    )


async def mark_stale_entries() -> int:
    """按 settings consolidate.staleAfterMonths 标记 stale，返回标记数。"""
    from agents.wiki.evolution.settings import get_setting

    stale_months = int(get_setting("consolidate.staleAfterMonths", 3))
    now = datetime.now(timezone.utc)
    marked = 0
    for entry in list_wiki_entries():
        if entry.meta.get("stale") == "true":
            continue
        ts = _entry_freshness_key(entry)
        if not ts:
            continue
        try:
            last = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            continue
        if (now - last).days > stale_months * 30:
            _mark_entry_stale(entry)
            marked += 1
    return marked


def _mark_entry_stale(entry: WikiEntry) -> None:
    from agents.core.frontmatter import parse_frontmatter, format_frontmatter

    filepath = get_wiki_dir() / entry.rel_path
    if not filepath.exists():
        return
    result = parse_frontmatter(filepath.read_text())
    result.meta["stale"] = "true"
    result.meta["stale_since"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    filepath.write_text(format_frontmatter(result.meta, result.body))


# ── 选材 ──

async def _select_material(
    changes: list[tuple[str, str]],
) -> list[dict[str, str]]:
    """A/M 全文 + 关联条目 top3 + stale 清单，预算 SELECTION_BUDGET。"""
    material: dict[str, dict[str, str]] = {}

    def _add(entry: WikiEntry, status: str) -> None:
        if len(material) >= SELECTION_BUDGET or entry.rel_path in material:
            return
        material[entry.rel_path] = {
            "path": entry.rel_path,
            "type": entry.type,
            "name": entry.name,
            "status": status,
            "content": entry.content[:ENTRY_CLIP_CHARS],
        }

    changed_entries: list[WikiEntry] = []
    for status, path in changes:
        if status == "D":
            continue
        entry = read_wiki_entry(path)
        if entry:
            _add(entry, status)
            changed_entries.append(entry)

    # 关联条目：与 A/M 条目语义相近的现有条目
    try:
        for entry in changed_entries[:RELATED_LOOKUPS]:
            if len(material) >= SELECTION_BUDGET:
                break
            try:
                similar = await preflight_wiki_search(entry.content[:2000], entry.type, top_k=3)
            except Exception as e:
                logger.warning("[consolidate] preflight failed for %s: %s", entry.rel_path, e)
                continue
            for related, _score in similar:
                _add(related, "related")
    except Exception as e:
        logger.warning("[consolidate] related lookup error: %s", e)

    # stale 清单（排序：usage_count DESC, freshness DESC 之外取最冷）
    stale = [e for e in list_wiki_entries() if e.meta.get("stale") == "true"]
    stale.sort(key=_entry_freshness_key)
    for entry in stale[:STALE_CAP]:
        _add(entry, "stale")

    return list(material.values())


# ── 动作执行 ──

def _set_meta(entry_path: str, updates: dict[str, str]) -> bool:
    from agents.core.frontmatter import parse_frontmatter, format_frontmatter

    filepath = get_wiki_dir() / entry_path
    if not filepath.exists():
        return False
    result = parse_frontmatter(filepath.read_text())
    result.meta.update(updates)
    filepath.write_text(format_frontmatter(result.meta, result.body))
    return True


def _rewrite_body(entry_path: str, new_content: str) -> bool:
    from agents.core.frontmatter import parse_frontmatter, format_frontmatter

    filepath = get_wiki_dir() / entry_path
    if not filepath.exists():
        return False
    result = parse_frontmatter(filepath.read_text())
    result.meta["modified"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    result.meta["last_refreshed"] = result.meta["modified"]
    result.meta.pop("stale", None)
    result.meta.pop("stale_since", None)
    filepath.write_text(format_frontmatter(result.meta, new_content))
    return True


def _archive(entry_path: str, *, superseded_by: str = "", merged_into: str = "") -> bool:
    from agents.wiki.evolution.lifecycle import disable_entry, truncate_archived_body

    entry = read_wiki_entry(entry_path)
    if not entry:
        return False
    extra = {}
    if superseded_by:
        extra["superseded_by"] = superseded_by
    if merged_into:
        extra["merged_into"] = merged_into
    if extra and not _set_meta(entry_path, extra):
        return False
    disable_entry(entry)
    truncate_archived_body(entry)
    return True


def _execute_actions(actions: list[dict]) -> dict[str, int]:
    stats = {"applied": 0, "skipped": 0}
    seen_targets: set[str] = set()

    for action in actions:
        name = action.get("action", "")
        targets = [t for t in action.get("targets", []) if isinstance(t, str)]
        into = str(action.get("into", "") or "")
        content = str(action.get("content", "") or "")
        reason = str(action.get("reason", ""))[:120]

        if name not in VALID_ACTIONS or name == "keep" or not targets:
            stats["skipped"] += 1
            continue
        if any(t in seen_targets for t in targets):
            logger.warning("[consolidate] skip action, target reuse: %s", targets)
            stats["skipped"] += 1
            continue
        if name in ("merge", "supersede") and not into:
            logger.warning("[consolidate] skip %s without into: %s", name, targets)
            stats["skipped"] += 1
            continue

        try:
            if name == "rewrite":
                if not content or not _rewrite_body(targets[0], content):
                    stats["skipped"] += 1
                    continue
            elif name == "archive":
                if not all(_archive(t) for t in targets):
                    stats["skipped"] += 1
                    continue
            elif name == "supersede":
                if not all(_archive(t, superseded_by=into) for t in targets if t != into):
                    stats["skipped"] += 1
                    continue
            elif name == "merge":
                others = [t for t in targets if t != into]
                if content:
                    if not _rewrite_body(into, content):
                        stats["skipped"] += 1
                        continue
                else:
                    into_entry = read_wiki_entry(into)
                    if not into_entry:
                        stats["skipped"] += 1
                        continue
                    for other in others:
                        other_entry = read_wiki_entry(other)
                        if other_entry:
                            merge_wiki_entry(into_entry, other_entry.content, mode="append")
                if not all(_archive(t, merged_into=into) for t in others):
                    stats["skipped"] += 1
                    continue

            seen_targets.update(targets)
            if into:
                seen_targets.add(into)
            stats["applied"] += 1
            logger.info("[consolidate] %s %s -> %s (%s)", name, targets, into or "-", reason)
        except Exception as e:
            logger.warning("[consolidate] action %s failed on %s: %s: %s", name, targets, type(e).__name__, e)
            stats["skipped"] += 1

    return stats


def _verify_targets(actions: list[dict]) -> list[str]:
    """校验：动作涉及的文件仍存在且 frontmatter 可解析。返回损坏清单。"""
    from agents.core.frontmatter import parse_frontmatter

    broken = []
    for action in actions:
        paths = list(action.get("targets", []))
        if action.get("into"):
            paths.append(action["into"])
        for p in paths:
            filepath = get_wiki_dir() / str(p)
            if not filepath.exists():
                broken.append(str(p))
                continue
            try:
                parse_frontmatter(filepath.read_text())
            except Exception:
                broken.append(str(p))
    return broken


# ── 主流程 ──

async def run_consolidate(side_query: Any) -> dict[str, Any]:
    """执行一次完整整理。失败不推进状态，下次触发重试。"""
    try:
        head = _head_commit()
        if not head:
            logger.info("[consolidate] wiki repo has no commits, skip")
            return {"status": "skipped", "reason": "no_commits"}

        state = load_consolidate_state()
        base = state.get("last_consolidate_commit")
        changes = changed_entry_files(base, head)

        if not changes:
            # 幂等：diff 为空直接成功推进
            save_consolidate_state({
                **state,
                "last_consolidate_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "last_consolidate_commit": head,
            })
            return {"status": "noop", "changes": 0}

        if len(changes) < MIN_CHANGED_ENTRIES:
            logger.info("[consolidate] only %d changes (< %d), defer", len(changes), MIN_CHANGED_ENTRIES)
            return {"status": "deferred", "changes": len(changes)}

        marked = await mark_stale_entries()
        material = await _select_material(changes)
        if not material:
            return {"status": "skipped", "reason": "no_material"}

        raw = await side_query(
            CONSOLIDATE_V2_PROMPT,
            "Entries:\n" + json.dumps(material, ensure_ascii=False),
        )
        actions = _parse_actions(raw)
        if not actions:
            # LLM 判定无需动作：也算成功整理，推进状态
            _finish(head, {"applied": 0, "skipped": 0}, stale_marked=marked)
            return {"status": "clean", "changes": len(changes), "stale_marked": marked}

        stats = _execute_actions(actions)
        broken = _verify_targets(actions)
        if broken:
            logger.warning("[consolidate] broken targets after execution: %s", broken)

        _finish(head, stats, stale_marked=marked)
        return {
            "status": "done",
            "changes": len(changes),
            "stale_marked": marked,
            **stats,
            "broken": broken,
        }
    except Exception as e:
        logger.error("[consolidate] run failed: %s: %s", type(e).__name__, e)
        return {"status": "error", "error": f"{type(e).__name__}: {e}"}


def _parse_actions(raw: str) -> list[dict]:
    text = str(raw or "").strip()
    fenced = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text)
    if fenced:
        text = fenced.group(1).strip()
    match = re.search(r"\[[\s\S]*\]", text)
    if not match:
        return []
    try:
        items = json.loads(match.group(0))
    except json.JSONDecodeError as e:
        logger.warning("[consolidate] actions not valid JSON: %s", e)
        return []
    if not isinstance(items, list):
        return []
    return [i for i in items if isinstance(i, dict)]


def _finish(head: str, stats: dict[str, int], *, stale_marked: int) -> None:
    update_wiki_index()
    _git_commit(
        f"wiki: consolidate (applied={stats.get('applied', 0)}, "
        f"skipped={stats.get('skipped', 0)}, stale_marked={stale_marked})"
    )
    # 重读状态：update_wiki_index 可能刚写入 pruned_from_index
    save_consolidate_state({
        **load_consolidate_state(),
        "last_consolidate_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "last_consolidate_commit": head,
    })
