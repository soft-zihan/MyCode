"""Harness 组件回滚机制。

支持从 refinement history 或 skill evolution history 恢复任意历史版本。

回滚策略：
1. 查找目标 refinement event
2. 从 event.applied_edits 中提取 before 状态
3. 恢复文件内容和 HarnessEntry
4. 记录新的 rollback refinement event
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any, Optional

from agents._utils import safe_skill_slug as _safe_skill_slug
from .refinement import (
    HarnessEntry,
    HarnessScope,
    HarnessState,
    RefinementAction,
    RefinementEvent,
    RefinementKind,
    append_refinement_log,
    load_harness_state,
    save_harness_state,
    _utc_now,
)
from agents.observability.trace import trace_event


# ── 回滚单个 Entry ────────────────────────────────────────────────────────────


def rollback_entry(
    kind: RefinementKind,
    entry_id: str,
    *,
    target_version: Optional[int] = None,
) -> dict[str, Any]:
    """回滚单个 harness entry 到指定版本。

    Args:
        kind: 组件类型
        entry_id: entry ID
        target_version: 目标版本号（None 表示回滚到上一版本）

    Returns:
        回滚结果字典
    """
    state = load_harness_state()
    entry = state.get_entry(kind, entry_id)

    result: dict[str, Any] = {
        "kind": kind.value,
        "entry_id": entry_id,
        "success": False,
        "error": None,
    }

    if not entry:
        result["error"] = f"Entry {entry_id} not found"
        return result

    # 从 refinement history 中查找该 entry 的历史版本
    history = _find_entry_history(state, kind, entry_id)

    if not history:
        result["error"] = f"No history found for {entry_id}"
        return result

    # 确定目标版本
    if target_version is None:
        # 回滚到上一版本
        if len(history) < 2:
            result["error"] = "No previous version to rollback to"
            return result
        target = history[-2]
    else:
        target = next((h for h in history if h.get("version") == target_version), None)
        if not target:
            result["error"] = f"Version {target_version} not found"
            return result

    # 恢复文件内容
    file_path = Path(entry.path) if entry.path else None
    before_content = file_path.read_text(encoding="utf-8") if file_path and file_path.is_file() else None

    if file_path:
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(target.get("content", ""), encoding="utf-8")

    # 更新 entry
    entry.content = target.get("content", entry.content)
    entry.title = target.get("title", entry.title)
    entry.version += 1
    entry.updated_at = _utc_now()
    state.set_entry(entry)
    save_harness_state(state)

    # 记录回滚事件
    rollback_event = RefinementEvent(
        id=str(uuid.uuid4())[:8],
        trigger="rollback",
        summary=f"Rollback {kind.value}/{entry_id} to version {target.get('version')}",
        rationale=f"User requested rollback from version {entry.version - 1}",
        expected_outcome=f"Entry restored to version {target.get('version')}",
        applied_edits=[{
            "action": "update",
            "kind": kind.value,
            "entry_id": entry_id,
            "before": {"content": before_content, "version": entry.version - 1},
            "after": {"content": entry.content, "version": entry.version},
            "applied": True,
        }],
        scope=entry.scope,
        created_at=_utc_now(),
    )
    state.add_refinement(rollback_event)
    save_harness_state(state)
    append_refinement_log(rollback_event)

    trace_event(
        "rollback.entry",
        kind=kind.value,
        entry_id=entry_id,
        from_version=entry.version - 1,
        to_version=target.get("version"),
    )

    result["success"] = True
    result["from_version"] = entry.version - 1
    result["to_version"] = target.get("version")
    return result


def _find_entry_history(
    state: HarnessState,
    kind: RefinementKind,
    entry_id: str,
) -> list[dict[str, Any]]:
    """从 refinement history 中提取某 entry 的版本历史。"""
    history = []
    for event in state.refinements:
        for edit in event.applied_edits:
            if edit.get("kind") != kind.value:
                continue
            if edit.get("entry_id") != entry_id:
                continue
            if not edit.get("applied"):
                continue

            after = edit.get("after", {})
            if after.get("entry"):
                history.append({
                    "version": after["entry"].get("version"),
                    "content": after["entry"].get("content"),
                    "title": after["entry"].get("title"),
                    "updated_at": after["entry"].get("updated_at"),
                    "refinement_id": event.id,
                })
    return history


# ── 回滚 Skill（兼容现有 skill_evolution history）─────────────────────────────


def rollback_skill(
    skill_name: str,
    *,
    target_version: Optional[int] = None,
) -> dict[str, Any]:
    """回滚 skill 到指定版本。

    优先从 refinement history 查找，若未找到则从 skill_evolution history 查找。

    Args:
        skill_name: skill 名称
        target_version: 目标版本号（None 表示回滚到上一版本）

    Returns:
        回滚结果字典
    """
    result: dict[str, Any] = {
        "skill_name": skill_name,
        "success": False,
        "error": None,
    }

    # 先尝试从 refinement history 回滚
    entry_id = f"skill-{skill_name}"
    refinement_result = rollback_entry(RefinementKind.SKILL, entry_id, target_version=target_version)

    if refinement_result.get("success"):
        result["success"] = True
        result["source"] = "refinement_history"
        result["from_version"] = refinement_result.get("from_version")
        result["to_version"] = refinement_result.get("to_version")
        return result

    # 回退到 skill_evolution history
    from agents.skills.skill_file_ops import get_evolution_dir, HISTORY_DIR

    history_path = get_evolution_dir() / HISTORY_DIR / f"{_safe_skill_slug(skill_name)}.jsonl"
    if not history_path.is_file():
        result["error"] = f"No history found for skill {skill_name}"
        return result

    # 读取历史快照
    snapshots = []
    for line in history_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            snapshots.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    if not snapshots:
        result["error"] = f"History file is empty for skill {skill_name}"
        return result

    # 确定目标版本
    if target_version is None:
        if len(snapshots) < 2:
            result["error"] = "No previous version to rollback to"
            return result
        target = snapshots[-2]
    else:
        target = next((s for s in snapshots if s.get("version") == target_version), None)
        if not target:
            result["error"] = f"Version {target_version} not found in history"
            return result

    # 恢复 skill 文件
    skill_path = _resolve_skill_path(skill_name)
    if not skill_path:
        result["error"] = f"Cannot resolve path for skill {skill_name}"
        return result

    before_content = skill_path.read_text(encoding="utf-8") if skill_path.is_file() else None
    skill_path.parent.mkdir(parents=True, exist_ok=True)
    skill_path.write_text(target.get("content", ""), encoding="utf-8")

    trace_event(
        "rollback.skill",
        skill_name=skill_name,
        from_version=target.get("version", 0) + 1 if target.get("version") else None,
        to_version=target.get("version"),
        source="skill_evolution_history",
    )

    result["success"] = True
    result["source"] = "skill_evolution_history"
    result["to_version"] = target.get("version")
    result["restored_path"] = str(skill_path)
    return result


def _resolve_skill_path(skill_name: str) -> Optional[Path]:
    """解析 skill 文件路径。"""
    cwd = Path.cwd()

    # 项目级 skill
    project_path = cwd / ".bear" / "skills" / skill_name / "SKILL.md"
    if project_path.is_file():
        return project_path

    # 用户级 skill
    user_path = Path.home() / ".bear" / "skills" / skill_name / "SKILL.md"
    if user_path.is_file():
        return user_path

    # 返回项目级路径（用于创建）
    return project_path


# ── 回滚整个 Refinement Event ─────────────────────────────────────────────────


def rollback_refinement(refinement_id: str) -> dict[str, Any]:
    """回滚整个 refinement event（撤销其所有 edits）。

    Args:
        refinement_id: refinement event ID

    Returns:
        回滚结果字典
    """
    state = load_harness_state()

    # 查找目标 event
    target_event = next((r for r in state.refinements if r.id == refinement_id), None)
    if not target_event:
        return {"success": False, "error": f"Refinement {refinement_id} not found"}

    result: dict[str, Any] = {
        "refinement_id": refinement_id,
        "success": False,
        "rolled_back_edits": [],
    }

    # 逆序撤销每个 edit
    for edit in reversed(target_event.applied_edits):
        if not edit.get("applied"):
            continue

        kind_str = edit.get("kind", "skill")
        entry_id = edit.get("entry_id", "")
        before = edit.get("before", {})

        try:
            kind = RefinementKind(kind_str)
        except ValueError:
            continue

        # 恢复 before 状态
        if before.get("entry"):
            entry_data = before["entry"]
            entry = HarnessEntry.from_dict(entry_data)
            state.set_entry(entry)

        if before.get("file_content") is not None:
            file_path = Path(edit.get("path", ""))
            if file_path:
                file_path.parent.mkdir(parents=True, exist_ok=True)
                file_path.write_text(before["file_content"], encoding="utf-8")

        result["rolled_back_edits"].append({
            "kind": kind_str,
            "entry_id": entry_id,
            "restored": True,
        })

    # 记录回滚事件
    rollback_event = RefinementEvent(
        id=str(uuid.uuid4())[:8],
        trigger="rollback",
        summary=f"Rollback refinement {refinement_id}",
        rationale=f"User requested rollback of refinement {refinement_id}",
        expected_outcome=f"All edits from {refinement_id} reverted",
        applied_edits=result["rolled_back_edits"],
        scope=target_event.scope,
        created_at=_utc_now(),
        rollback_of=refinement_id,
    )
    state.add_refinement(rollback_event)
    save_harness_state(state)
    append_refinement_log(rollback_event)

    trace_event(
        "rollback.refinement",
        refinement_id=refinement_id,
        edits_count=len(result["rolled_back_edits"]),
    )

    result["success"] = True
    return result


# ── 查询历史版本 ───────────────────────────────────────────────────────────────


def list_versions(
    kind: RefinementKind,
    entry_id: str,
    *,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """列出某 entry 的历史版本。"""
    state = load_harness_state()
    history = _find_entry_history(state, kind, entry_id)
    return history[-limit:]


def list_skill_versions(
    skill_name: str,
    *,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """列出 skill 的历史版本（合并 refinement + skill_evolution history）。"""
    versions = []

    # 从 refinement history
    entry_id = f"skill-{skill_name}"
    refinement_versions = list_versions(RefinementKind.SKILL, entry_id, limit=limit)
    versions.extend(refinement_versions)

    # 从 skill_evolution history
    from agents.skills.skill_file_ops import get_evolution_dir, HISTORY_DIR

    history_path = get_evolution_dir() / HISTORY_DIR / f"{_safe_skill_slug(skill_name)}.jsonl"
    if history_path.is_file():
        for line in history_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                snapshot = json.loads(line)
                versions.append({
                    "version": snapshot.get("version"),
                    "content_preview": snapshot.get("content", "")[:200],
                    "updated_at": snapshot.get("time"),
                    "source": "skill_evolution_history",
                })
            except json.JSONDecodeError:
                continue

    # 按版本号排序
    versions.sort(key=lambda v: v.get("version") or 0, reverse=True)
    return versions[:limit]
