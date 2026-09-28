"""Wiki 工具 — remember 单一写入入口。

职责分离（学 Codex ad-hoc notes）：模型提意图，程序保格式，落库前强制去重。
所有 Agent 主动写入统一走 remember → preflight 单入口（B6 修复：不再绕过 preflight）。
"""

from __future__ import annotations

import asyncio
import json

from agents.wiki.redact import redact_secrets
from agents.wiki.store import (
    get_wiki_dir,
    merge_wiki_entry,
    write_wiki_entry,
    write_workflow_pattern,
)
from agents.wiki.wiki_consolidator import maybe_schedule_consolidate
from agents.wiki.wiki_manager import preflight_wiki_search

REMEMBER_TYPES = {"feedback", "user", "knowledge", "reference", "workflow_pattern"}

MERGE_REPLACE_THRESHOLD = 0.85
MERGE_APPEND_THRESHOLD = 0.70


def _error(message: str) -> str:
    return json.dumps({"action": "error", "error": message}, ensure_ascii=False)


def _prepare_remember_payload(
    wiki_type: str, inp: dict, description: str
) -> tuple[dict[str, str] | None, str | None]:
    """校验并脱敏输入；返回 (payload, error)，payload 含 content/description
    （workflow_pattern 另含 symptom/root_cause/workaround 供专用写入）。"""
    if wiki_type == "workflow_pattern":
        symptom = str(inp.get("symptom", "")).strip()
        root_cause = str(inp.get("root_cause", "")).strip()
        workaround = str(inp.get("workaround", "")).strip()
        if not (symptom and root_cause and workaround):
            return None, "workflow_pattern requires symptom, root_cause and workaround"
        symptom, root_cause, workaround = (
            redact_secrets(symptom), redact_secrets(root_cause), redact_secrets(workaround)
        )
        content = f"## Symptom\n{symptom}\n\n## Root cause\n{root_cause}\n\n## Workaround\n{workaround}"
        return {
            "content": content,
            "description": description or symptom[:80],
            "symptom": symptom,
            "root_cause": root_cause,
            "workaround": workaround,
        }, None

    content = str(inp.get("content", "")).strip()
    if not content:
        return None, "content is required"
    content = redact_secrets(content)
    return {"content": content, "description": description or content[:80]}, None


async def _try_merge_similar(similar: list, content: str, side_query) -> str | None:
    """高相似走 replace、中相似走 append 合并；未达阈值返回 None。"""
    if not similar:
        return None
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
    return None


async def _create_remember_entry(wiki_type: str, name: str, payload: dict[str, str]):
    """新建条目：workflow_pattern 走专用写入，其余走通用 write_wiki_entry。"""
    if wiki_type == "workflow_pattern":
        return await asyncio.to_thread(
            write_workflow_pattern,
            name=name,
            symptom=payload["symptom"],
            root_cause=payload["root_cause"],
            workaround=payload["workaround"],
            description=payload["description"],
            extra_meta={"source": "agent"},
        )
    return await asyncio.to_thread(
        write_wiki_entry,
        wiki_type=wiki_type,
        name=name,
        content=payload["content"],
        description=payload["description"],
        extra_meta={"source": "agent"},
    )


async def remember(inp: dict, side_query=None) -> str:
    """写入一条持久记忆，自动去重合并。

    Args:
        side_query: 可选，用于写入后触发 Phase 3 整理门槛检查。

    Returns:
        JSON 字符串 {action: created|merged|appended, path, message}
    """
    wiki_type = str(inp.get("wiki_type", "")).strip()
    name = str(inp.get("name", "")).strip()
    description = str(inp.get("description", "")).strip()

    if wiki_type not in REMEMBER_TYPES:
        return _error(
            f"wiki_type must be one of {sorted(REMEMBER_TYPES)}, got {wiki_type!r}"
        )
    if not name:
        return _error("name is required")

    payload, error = _prepare_remember_payload(wiki_type, inp, description)
    if error or payload is None:
        return _error(error or "invalid input")

    try:
        similar = await preflight_wiki_search(payload["content"], wiki_type)
    except Exception as e:
        print(f"[remember] preflight failed ({type(e).__name__}: {e}), falling back to create")
        similar = []

    try:
        merged = await _try_merge_similar(similar, payload["content"], side_query)
        if merged:
            return merged

        path = await _create_remember_entry(wiki_type, name, payload)
        _maybe_consolidate(side_query)
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
        if maybe_schedule_consolidate(side_query):
            print("[wiki_consolidate] scheduled after remember (threshold met)")
    except RuntimeError:
        # 无事件循环（同步测试环境）——跳过调度
        pass


def _result(action: str, path, top_entry, top_score: float) -> str:
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
