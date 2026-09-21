"""Wiki 工具 — remember 单一写入入口。

职责分离（学 Codex ad-hoc notes）：模型提意图，程序保格式，落库前强制去重。
所有 Agent 主动写入统一走 remember → preflight 单入口（B6 修复：不再绕过 preflight）。
"""

from __future__ import annotations

import asyncio
import json

REMEMBER_TYPES = {"feedback", "user", "knowledge", "reference", "workflow_pattern"}

MERGE_REPLACE_THRESHOLD = 0.85
MERGE_APPEND_THRESHOLD = 0.70


def _error(message: str) -> str:
    return json.dumps({"action": "error", "error": message}, ensure_ascii=False)


async def remember(inp: dict, side_query=None) -> str:
    """写入一条持久记忆，自动去重合并。

    Args:
        side_query: 可选，用于写入后触发 Phase 3 整理门槛检查。

    Returns:
        JSON 字符串 {action: created|merged|appended, path, message}
    """
    from agents.wiki.redact import redact_secrets
    from agents.wiki.wiki_manager import (
        merge_wiki_entry,
        preflight_wiki_search,
        write_wiki_entry,
        write_workflow_pattern,
    )

    wiki_type = str(inp.get("wiki_type", "")).strip()
    name = str(inp.get("name", "")).strip()
    description = str(inp.get("description", "")).strip()

    if wiki_type not in REMEMBER_TYPES:
        return _error(
            f"wiki_type must be one of {sorted(REMEMBER_TYPES)}, got {wiki_type!r}"
        )
    if not name:
        return _error("name is required")

    if wiki_type == "workflow_pattern":
        symptom = str(inp.get("symptom", "")).strip()
        root_cause = str(inp.get("root_cause", "")).strip()
        workaround = str(inp.get("workaround", "")).strip()
        if not (symptom and root_cause and workaround):
            return _error("workflow_pattern requires symptom, root_cause and workaround")
        symptom, root_cause, workaround = (
            redact_secrets(symptom), redact_secrets(root_cause), redact_secrets(workaround)
        )
        content = f"## Symptom\n{symptom}\n\n## Root cause\n{root_cause}\n\n## Workaround\n{workaround}"
        description = description or symptom[:80]
    else:
        content = str(inp.get("content", "")).strip()
        if not content:
            return _error("content is required")
        content = redact_secrets(content)
        description = description or content[:80]

    try:
        similar = await preflight_wiki_search(content, wiki_type)
    except Exception as e:
        print(f"[remember] preflight failed ({type(e).__name__}: {e}), falling back to create")
        similar = []

    try:
        if similar:
            top_entry, top_score = similar[0]
            if top_score >= MERGE_REPLACE_THRESHOLD:
                path = await asyncio.to_thread(
                    merge_wiki_entry, top_entry, content, mode="replace"
                )
                _maybe_consolidate(side_query)
                return _result("merged", path, top_entry, top_score)
            if top_score >= MERGE_APPEND_THRESHOLD:
                path = await asyncio.to_thread(
                    merge_wiki_entry, top_entry, content, mode="append"
                )
                _maybe_consolidate(side_query)
                return _result("appended", path, top_entry, top_score)

        if wiki_type == "workflow_pattern":
            path = await asyncio.to_thread(
                write_workflow_pattern,
                name=name,
                symptom=symptom,
                root_cause=root_cause,
                workaround=workaround,
                description=description,
                extra_meta={"source": "agent"},
            )
        else:
            path = await asyncio.to_thread(
                write_wiki_entry,
                wiki_type=wiki_type,
                name=name,
                content=content,
                description=description,
                extra_meta={"source": "agent"},
            )
        _maybe_consolidate(side_query)
        from agents.wiki.wiki_manager import get_wiki_dir
        rel = str(path.relative_to(get_wiki_dir()))
        return json.dumps(
            {"action": "created", "path": rel, "message": f"Created new {wiki_type} entry: {rel}"},
            ensure_ascii=False,
        )
    except Exception as e:
        return _error(f"{type(e).__name__}: {e}")


def _maybe_consolidate(side_query) -> None:
    """Phase 3 触发点：remember 写入后检查整理门槛。"""
    if side_query is None:
        return
    try:
        from agents.wiki.wiki_consolidator import maybe_schedule_consolidate
        if maybe_schedule_consolidate(side_query):
            print("[wiki_consolidate] scheduled after remember (threshold met)")
    except RuntimeError:
        # 无事件循环（同步测试环境）——跳过调度
        pass


def _result(action: str, path, top_entry, top_score: float) -> str:
    from agents.wiki.wiki_manager import get_wiki_dir
    rel = str(path.relative_to(get_wiki_dir()))
    return json.dumps(
        {
            "action": action,
            "path": rel,
            "message": (
                f"Content {action} into existing entry '{top_entry.name}' "
                f"({top_entry.rel_path}, similarity {top_score:.2f}). "
                f"Tell the user which entry was updated."
            ),
        },
        ensure_ascii=False,
    )
