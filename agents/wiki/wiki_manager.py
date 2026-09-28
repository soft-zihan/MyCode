"""Wiki 召回编排 — preflight 预检索、side-query 选择、注入格式化、编译触发、prompt 段。

M4 解环：存储原语（条目 CRUD/git/索引）已提取至 agents/wiki/store.py 叶子层，
本模块只保留编排逻辑，依赖单向：wiki_manager → recall / wiki_compiler / store。
原 wiki_manager ↔ wiki_commit/recall/wiki_compiler 三环消除，函数内延迟 import 清零。
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path
from typing import Any

from agents.observability.trace import trace_span
from agents.wiki.evolution.recall import hybrid_recall
from agents.wiki.store import (
    WikiEntry,
    get_wiki_dir,
    increment_applied_count,
    is_skill_stale,
    list_wiki_entries,
    load_wiki_index,
    read_wiki_entry,
)
from agents.wiki.wiki_compiler import compile_to_skill


async def preflight_wiki_search(
    content: str,
    wiki_type: str,
    top_k: int = 5,
) -> list[tuple[WikiEntry, float]]:
    """写入前预检索，避免重复创建相似条目。

    Returns:
        [(entry, score), ...] 按分数降序
    """
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
    with trace_span(
        "skill.compile",
        metadata={"pattern_rel_path": pattern_rel_path},
    ) as span:
        try:
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
            print(f"[wiki_select] no entries (wiki_dir={get_wiki_dir()}), took {time.time()-t0:.2f}s")
            return []

        candidates = [e for e in entries if e.rel_path not in already_surfaced]
        if not candidates:
            if span:
                span.add_metadata(recalled_count=0, reason="all_already_surfaced")
            print(f"[wiki_select] all entries already surfaced, took {time.time()-t0:.2f}s")
            return []

        try:
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
                    # BC-35：召回内容写入 span output——评审侧（judge）只见
                    # observations，无此上下文会把"遵循记忆规则的回答"误判为错误
                    span.update(output="\n---\n".join(
                        f"[{e.rel_path}]\n{e.content[:500]}" for e in result
                    )[:4000])
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
            text = await side_query(
                SELECT_WIKI_PROMPT,
                f"Query: {query}\n\nAvailable wiki entries:\n{manifest}",
            )
            match = re.search(r"\{[\s\S]*\}", text)
            selected_paths: list[str] = []
            if match:
                try:
                    parsed = json.loads(match.group(0))
                    selected_paths = parsed.get("selected_entries", [])
                except Exception:
                    selected_paths = []
            if not selected_paths:
                selected_paths = [e.rel_path for e in candidates if e.rel_path in text]

            by_path = {e.rel_path: e for e in candidates}
            selected = [by_path[p] for p in selected_paths if p in by_path][:5]

            if not selected:
                # side_query 空返回（如推理模型 reasoning 吃光 max_tokens）时的确定性兜底：
                # 按 query 词与 name/description/content 的重叠度取 top5，0 分不注入
                selected = _keyword_fallback_select(query, candidates)
                if selected:
                    print(f"[wiki_select] side_query empty, keyword fallback selected {len(selected)} entries")

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


WIKI_RECALL_COOLDOWN_TURNS = 5


def start_wiki_prefetch(agent: Any, user_message: str, side_query: Any) -> None:
    """启动本轮 wiki 召回预取（U0 从 Agent 迁入；U2 后子代理会话落盘再评估放开）。

    agent 需具备：is_sub_agent/_wiki_prefetch/_wiki_prefetch_consumed/
    _wiki_surfaced_at/_turn_number 状态（Agent 门面 start_wiki_prefetch 委托至此）。
    """
    if agent.is_sub_agent:
        return
    if agent._wiki_prefetch is not None:
        # BC-8：闩锁只在上一轮 prefetch 仍在途或尚未消费时生效。
        # 已消费则清空引用允许本轮重新召回——否则整个 session 只有第一轮
        # 会召回，中途 remember 写入的条目对后续轮次永远不可见。
        if not agent._wiki_prefetch.done() or not agent._wiki_prefetch_consumed:
            return
        agent._wiki_prefetch = None
    cooled_wiki_paths = {
        path for path, turn in agent._wiki_surfaced_at.items()
        if agent._turn_number - turn < WIKI_RECALL_COOLDOWN_TURNS
    }
    agent._wiki_prefetch_consumed = False
    agent._wiki_prefetch = asyncio.create_task(
        select_relevant_wiki_entries(user_message, side_query, cooled_wiki_paths)
    )


def _keyword_fallback_select(query: str, candidates: list[WikiEntry], limit: int = 5) -> list[WikiEntry]:
    """确定性关键词兜底选择：query 分词与条目文本重叠计数，0 分不选。"""
    q = query.lower()
    q_tokens = {t for t in re.split(r"[\s,，。.:：;；!！?？/\\-]+", q) if len(t) >= 2}
    # CJK 无空格分词：补字符 bigram，保证中文 query 可匹配
    cjk = re.sub(r"[^\u4e00-\u9fff]", "", q)
    q_tokens.update(cjk[i:i + 2] for i in range(len(cjk) - 1))
    if not q_tokens:
        return []
    scored: list[tuple[int, WikiEntry]] = []
    for e in candidates:
        text = f"{e.name} {e.meta.get('description', '')} {e.content[:500]}".lower()
        score = sum(1 for t in q_tokens if t in text)
        if score > 0:
            scored.append((score, e))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [e for _, e in scored[:limit]]


def format_wiki_for_injection(entries: list[WikiEntry]) -> str:
    parts = []
    for e in entries:
        parts.append(
            f"<system-reminder>\nWiki ({e.type}): {e.rel_path}\n\n{e.content}\n"
            f"[若本次回复实际使用了该条目，在回复末尾输出 <wiki-citation>{e.rel_path}</wiki-citation>]"
            f"\n</system-reminder>"
        )
    return "\n\n".join(parts)


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
