"""全 Harness 进化执行引擎。

接收 RefinementProposal，应用 edits 到四类 harness 组件：
- prompt notes (.mycode/prompts/*.md)
- memory entries (.mycode/memories/*.md)
- skills (.mycode/skills/*/SKILL.md)
- subagent specs (.mycode/agents/*.md)

设计参考：Prime Agent / HCL (arXiv:2605.09998) 的 guarded harness evolution。
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any, Optional

from .refinement import (
    HarnessEntry,
    HarnessScope,
    HarnessState,
    RefinementAction,
    RefinementEdit,
    RefinementEvent,
    RefinementKind,
    RefinementProposal,
    append_refinement_log,
    generate_entry_id,
    load_harness_state,
    resolve_component_path,
    save_harness_state,
    _utc_now,
)
from agents.observability.trace import trace_event


def _ensure_dir(path: Path) -> None:
    """确保文件所在目录存在。"""
    path.parent.mkdir(parents=True, exist_ok=True)


def _write_component_file(path: Path, content: str) -> None:
    """写入组件文件。"""
    _ensure_dir(path)
    path.write_text(content, encoding="utf-8")


def _delete_component_file(path: Path) -> bool:
    """删除组件文件。"""
    if path.is_file():
        path.unlink()
        return True
    return False


def _read_component_file(path: Path) -> Optional[str]:
    """读取组件文件内容。"""
    if path.is_file():
        return path.read_text(encoding="utf-8")
    return None


# ── 应用单个 Edit ─────────────────────────────────────────────────────────────


def apply_edit(
    edit: RefinementEdit,
    state: HarnessState,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """应用单个进化编辑。

    Returns:
        包含 before/after 状态的字典，用于审计。
    """
    result: dict[str, Any] = {
        "action": edit.action.value,
        "kind": edit.kind.value,
        "applied": False,
        "error": None,
    }

    # 确定目标 ID 和路径
    if edit.target_id:
        entry_id = edit.target_id
    elif edit.title:
        entry_id = generate_entry_id(edit.kind, edit.title)
    else:
        result["error"] = "Missing target_id or title"
        return result

    if edit.path:
        file_path = Path(edit.path)
    else:
        file_path = resolve_component_path(edit.kind, entry_id.split("-", 1)[-1] if "-" in entry_id else entry_id)

    result["entry_id"] = entry_id
    result["path"] = str(file_path)

    # 获取 before 状态
    before_entry = state.get_entry(edit.kind, entry_id)
    before_content = _read_component_file(file_path) if file_path.is_file() else None
    result["before"] = {
        "entry": before_entry.to_dict() if before_entry else None,
        "file_content": before_content,
    }

    if dry_run:
        result["applied"] = True
        return result

    # 执行编辑
    try:
        if edit.action == RefinementAction.CREATE:
            if before_entry:
                result["error"] = f"Entry {entry_id} already exists"
                return result
            if file_path.is_file():
                result["error"] = f"File {file_path} already exists"
                return result

            # 创建新 entry
            now = _utc_now()
            new_entry = HarnessEntry(
                id=entry_id,
                kind=edit.kind,
                title=edit.title or entry_id,
                content=edit.content or "",
                path=str(file_path),
                scope=HarnessScope.LOCAL,
                source="harness_evolution",
                created_at=now,
                updated_at=now,
                version=1,
                metadata=edit.metadata or {},
            )
            state.set_entry(new_entry)
            _write_component_file(file_path, edit.content or "")
            result["applied"] = True
            result["after"] = {
                "entry": new_entry.to_dict(),
                "file_content": edit.content,
            }
            trace_event(
                "refinement.create",
                refinement_kind=edit.kind.value,
                entry_id=entry_id,
                path=str(file_path),
            )

        elif edit.action == RefinementAction.UPDATE:
            if not before_entry and not before_content:
                result["error"] = f"Entry {entry_id} does not exist"
                return result

            # 更新 entry
            now = _utc_now()
            if before_entry:
                before_entry.content = edit.content or before_entry.content
                before_entry.title = edit.title or before_entry.title
                before_entry.updated_at = now
                before_entry.version += 1
                if edit.metadata:
                    before_entry.metadata.update(edit.metadata)
                updated_entry = before_entry
            else:
                # 文件存在但没有 entry 记录，创建 entry
                updated_entry = HarnessEntry(
                    id=entry_id,
                    kind=edit.kind,
                    title=edit.title or entry_id,
                    content=edit.content or before_content or "",
                    path=str(file_path),
                    scope=HarnessScope.LOCAL,
                    source="harness_evolution",
                    created_at=now,
                    updated_at=now,
                    version=1,
                    metadata=edit.metadata or {},
                )
            state.set_entry(updated_entry)
            _write_component_file(file_path, edit.content or before_content or "")
            result["applied"] = True
            result["after"] = {
                "entry": updated_entry.to_dict(),
                "file_content": edit.content or before_content,
            }
            trace_event(
                "refinement.update",
                refinement_kind=edit.kind.value,
                entry_id=entry_id,
                path=str(file_path),
                version=updated_entry.version,
            )

        elif edit.action == RefinementAction.DELETE:
            if not before_entry and not before_content:
                # 已经不存在，视为成功
                result["applied"] = True
                result["after"] = {"entry": None, "file_content": None}
                return result

            state.delete_entry(edit.kind, entry_id)
            _delete_component_file(file_path)
            result["applied"] = True
            result["after"] = {"entry": None, "file_content": None}
            trace_event(
                "refinement.delete",
                refinement_kind=edit.kind.value,
                entry_id=entry_id,
                path=str(file_path),
            )

    except Exception as e:
        result["error"] = str(e)

    return result


# ── 应用完整 Proposal ─────────────────────────────────────────────────────────


def apply_proposal(
    proposal: RefinementProposal,
    *,
    trigger: str = "manual",
    scope: HarnessScope = HarnessScope.LOCAL,
    dry_run: bool = False,
) -> RefinementEvent:
    """应用完整的进化提案。

    Args:
        proposal: LLM 生成的进化提案
        trigger: 触发方式（manual/auto/turn_interval/compact）
        scope: 作用域（local/global）
        dry_run: 是否只模拟不实际执行

    Returns:
        RefinementEvent 记录本次进化的完整审计信息
    """
    state = load_harness_state()
    event_id = str(uuid.uuid4())[:8]
    now = _utc_now()

    applied_edits: list[dict[str, Any]] = []

    for edit in proposal.edits:
        result = apply_edit(edit, state, dry_run=dry_run)
        applied_edits.append(result)

    # 创建进化事件记录
    event = RefinementEvent(
        id=event_id,
        trigger=trigger,
        summary=proposal.summary,
        rationale=proposal.rationale,
        expected_outcome=proposal.expected_outcome,
        applied_edits=applied_edits,
        scope=scope,
        created_at=now,
    )

    if not dry_run:
        state.add_refinement(event)
        save_harness_state(state)
        append_refinement_log(event)

    trace_event(
        "refinement.apply",
        event_id=event_id,
        trigger=trigger,
        scope=scope.value,
        edits_count=len(proposal.edits),
        applied_count=sum(1 for e in applied_edits if e.get("applied")),
        error_count=sum(1 for e in applied_edits if e.get("error")),
        dry_run=dry_run,
    )

    return event


# ── 查询与概览 ─────────────────────────────────────────────────────────────────


def get_harness_overview(
    *,
    kind: Optional[RefinementKind] = None,
    scope: Optional[HarnessScope] = None,
    limit: int = 50,
) -> dict[str, Any]:
    """获取 harness 状态概览。"""
    state = load_harness_state()
    entries = state.list_entries(kind)

    if scope:
        entries = [e for e in entries if e.scope == scope]

    # 按更新时间排序
    entries.sort(key=lambda e: e.updated_at or e.created_at, reverse=True)
    entries = entries[:limit]

    return {
        "schema_version": state.schema_version,
        "total_entries": len(state.list_entries()),
        "entries_by_kind": {
            k.value: len(v) for k, v in state.entries.items()
        },
        "entries": [e.to_dict() for e in entries],
        "recent_refinements": [r.to_dict() for r in state.refinements[-10:]],
    }


def get_refinement_history(
    *,
    limit: int = 20,
    trigger: Optional[str] = None,
) -> list[dict[str, Any]]:
    """获取进化历史。"""
    state = load_harness_state()
    refinements = state.refinements

    if trigger:
        refinements = [r for r in refinements if r.trigger == trigger]

    refinements = refinements[-limit:]
    return [r.to_dict() for r in reversed(refinements)]


# ── 与现有 skill evolution 集成 ────────────────────────────────────────────────


async def refine_from_skill_candidate(
    *,
    candidate_name: str,
    candidate_description: str,
    candidate_instructions: str,
    evidence: str = "",
    existing_skills: dict[str, Any] | None = None,
) -> RefinementEvent:
    """从现有的 skill candidate 创建 RefinementProposal 并应用。

    这是与 online_skill_evolution.py 的桥接函数。
    """
    edit = RefinementEdit(
        action=RefinementAction.CREATE,
        kind=RefinementKind.SKILL,
        title=candidate_name,
        content=candidate_instructions,
        reason=evidence or f"Extracted from conversation: {candidate_description}",
        metadata={
            "description": candidate_description,
            "source": "online_skill_evolution",
        },
    )

    proposal = RefinementProposal(
        summary=f"Create skill: {candidate_name}",
        rationale=evidence or candidate_description,
        edits=[edit],
        expected_outcome=f"Skill {candidate_name} available for future tasks",
    )

    return apply_proposal(proposal, trigger="auto")
