"""ContextCompressor — 两级上下文压缩管线。

职责：
- 策略 A：工具调用折叠（上下文利用率超过配置阈值或空闲超时）
- 策略 B：会话折叠（工具折叠后仍高于目标水位）

折叠通过追加 events_hidden 事件实现，原始内容保留在事件日志中。
摘要作为独立事件（tool_folded / session_folded）插入。

会话折叠会合并上一次会话笔记，避免重复生成浅层摘要。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Callable, Awaitable

from .context_events import (
    build_groups_transcript,
    build_message_groups,
    collect_hidden_seqs,
    estimate_event_tokens,
    estimate_visible_message_tokens,
    estimate_group_tokens,
    find_session_fold_cut,
    find_tool_fold_indices,
    latest_session_notes,
)
from .session_memory import (
    clip_text,
    fallback_folded_memory,
    format_folded_memory,
)
from agents.core.workspace import _current_workspace
from agents.observability.trace import trace_span
from agents.wiki.pipeline import on_session_folded

logger = logging.getLogger(__name__)

SESSION_FOLD_THRESHOLD = 0.40
# 压缩对比实验臂（GAIA 消融）：full=现状双层；truncate=朴素硬截断（无摘要，
# 同触发点同目标水位，直接隐藏最旧消息组）；tool_only=仅工具结果折叠；
# session_only=仅会话折叠。实验臂只保留利用率触发，禁用 idle 触发——
# idle 折叠会连带执行另一种机制，污染单臂对比。
COMPRESSION_ARMS = {"full", "truncate", "tool_only", "session_only"}
KEEP_RECENT_TOOL_ROUNDS = 3
KEEP_RECENT_DIALOG_ROUNDS = 2
KEEP_RECENT_TRAJECTORY_TOOL_ROUNDS = 5
# v2 conversation-checkpoint 双层结构：折叠摘要 + 最近 ~15k tokens 无损尾部
# （compaction.ts:41）。小窗口（测试/消融臂）按 35% 窗口比例退化，防预算吞没
# 全部内容；轮次下限（KEEP_RECENT_*）语义保持不变。
CHECKPOINT_TAIL_TOKEN_BUDGET = 15_000
CHECKPOINT_TAIL_WINDOW_RATIO = 0.35
IDLE_TIMEOUT_S = 5 * 60
# 任务边界折叠门：有 in_progress 任务时把折叠推迟到硬顶。
# 硬顶 = min(阈值 + OFFSET, MAX)。MAX 的存在是为了绝不让上下文贴到窗口边缘。
# 取值待 A/B 校准（spec §十五），本处只落机制。
FOLD_DEFER_CEILING_OFFSET = 0.10
FOLD_DEFER_CEILING_MAX = 0.95
TOOL_ABSTRACT_CHAR_LIMIT = 1200
TOOL_ABSTRACT_INPUT_CHAR_LIMIT = 8000
TOOL_ABSTRACT_BATCH_CHAR_LIMIT = 50000
ASSISTANT_TEXT_CHAR_LIMIT = 800

SideQueryFn = Callable[[str, str], Awaitable[str]]

TOOL_ABSTRACT_SYSTEM = """You compress old tool results for an AI coding agent.
Return only valid JSON:
{"abstracts": [{"call_id": "...", "abstract": "..."}]}

Rules:
- Preserve file paths, commands, important outputs, test results, errors, decisions, and unresolved issues.
- Preserve exact identifiers verbatim (never paraphrase or shorten): sub-agent session_id values from <subagent session_id=...> tags, job IDs, URLs, branch names, keys/tokens shown in outputs. Losing them breaks resumption.
- Keep each abstract concise and under 1200 characters.
- Do not invent details that are absent from the tool result.
"""

# 会话笔记和项目知识编译的 system prompt - 从文件加载
_PROMPTS_DIR = Path(__file__).parent.parent / "prompts" / "side_query"
COMPILE_SESSION_NOTES_AND_KNOWLEDGE_SYSTEM = (_PROMPTS_DIR / "compile_session.txt").read_text(encoding="utf-8")

# U5a：session_notes 结构校验（对齐 v2 compaction.ts 的模板命中检查 + 纠正重试）
REQUIRED_NOTES_SECTIONS = (
    "## Objective",
    "## Requirements",
    "## Decisions",
    "## Work State",
    "## Next Move",
    "## Relevant Files",
    "## Important Context",
)


def validate_session_notes(notes: str) -> list[str]:
    """返回缺失的必需小节标题列表（空列表 = 结构合格）。"""
    return [s for s in REQUIRED_NOTES_SECTIONS if s not in notes]


def _count_hidden_seqs(session: Any) -> int:
    return len(collect_hidden_seqs(session.events))


def _finish_compaction_span(
    span: Any,
    session: Any,
    hidden_before: int,
    folded: bool,
    session_folded: bool,
) -> None:
    """压缩结束时在 span 上记录折叠统计（Langfuse 可过滤 metadata）。"""
    if span is None:
        return
    hidden_after = _count_hidden_seqs(session)
    seqs_hidden = hidden_after - hidden_before
    total = len(session.events)
    metadata: dict[str, Any] = {
        "folded": folded,
        "session_fold": session_folded,
        "seqs_hidden": seqs_hidden,
        "hidden_total": hidden_after,
    }
    if total:
        metadata["retention_ratio"] = round((total - hidden_after) / total, 3)
    span.add_metadata(**metadata)


class ContextCompressor:
    def __init__(
        self,
        *,
        effective_window: int,
        tool_fold_threshold: float,
        session_fold_threshold: float = SESSION_FOLD_THRESHOLD,
        keep_recent_tool_rounds: int = KEEP_RECENT_TOOL_ROUNDS,
        keep_recent_dialog_rounds: int = KEEP_RECENT_DIALOG_ROUNDS,
        keep_recent_trajectory_tool_rounds: int = KEEP_RECENT_TRAJECTORY_TOOL_ROUNDS,
        idle_timeout_seconds: int = IDLE_TIMEOUT_S,
        tool_abstract_char_limit: int = TOOL_ABSTRACT_CHAR_LIMIT,
        tool_abstract_input_char_limit: int = TOOL_ABSTRACT_INPUT_CHAR_LIMIT,
        tool_abstract_batch_char_limit: int = TOOL_ABSTRACT_BATCH_CHAR_LIMIT,
        assistant_text_char_limit: int = ASSISTANT_TEXT_CHAR_LIMIT,
        wiki_enabled: bool = True,
        arm: str = "full",
    ) -> None:
        if arm not in COMPRESSION_ARMS:
            raise ValueError(f"compression arm must be one of {sorted(COMPRESSION_ARMS)}, got {arm!r}")
        self.arm = arm
        self.wiki_enabled = wiki_enabled
        self.tool_fold_threshold = tool_fold_threshold
        self.session_fold_threshold = session_fold_threshold
        self.keep_recent_tool_rounds = keep_recent_tool_rounds
        self.keep_recent_dialog_rounds = keep_recent_dialog_rounds
        self.keep_recent_trajectory_tool_rounds = keep_recent_trajectory_tool_rounds
        self.idle_timeout_seconds = idle_timeout_seconds
        self.tool_abstract_char_limit = tool_abstract_char_limit
        self.tool_abstract_input_char_limit = tool_abstract_input_char_limit
        self.tool_abstract_batch_char_limit = tool_abstract_batch_char_limit
        self.assistant_text_char_limit = assistant_text_char_limit
        self.effective_window = effective_window

        self._fold_last_time: float = 0.0
        self._fold_count: int = 0
        self._tool_fold_count: int = 0
        self._session_fold_count: int = 0
        self._truncation_count: int = 0
        self._background_tasks: set[asyncio.Task[Any]] = set()

    def _defer_ceiling(self) -> float:
        """任务边界门的硬顶 = min(阈值 + OFFSET, MAX)。

        MAX 那一半是「绝不让上下文贴到窗口边缘」的安全属性：阈值配到 0.85 以上时
        阈值 + OFFSET 会超过 0.95，此时以 MAX 为准。公式刻意只在这一处出现，
        _should_compress 与 _fold_trigger_in_reach 共用它，免得两处漂移。
        """
        return min(self.tool_fold_threshold + FOLD_DEFER_CEILING_OFFSET,
                   FOLD_DEFER_CEILING_MAX)

    def _fold_trigger_in_reach(self, utilization: float, idle_seconds: float) -> bool:
        """利用率/空闲时间是否逼近**任何**一个触发点（含硬顶）。

        只服务一个问题：门探针值不值得解析（解析 = 一次任务清单磁盘读）。它不决定
        折叠。三条并列覆盖全部触发线：阈值、硬顶（阈值 ≥ MAX 的退化配置下硬顶比
        阈值低，硬顶自己就是一条触发线）、full 臂的空闲超时。三条都不成立时，
        _should_compress 无论门开门关都返回 False —— 所以那里不必读任务清单。
        """
        return (
            utilization > self.tool_fold_threshold
            or utilization > self._defer_ceiling()
            or (self.arm == "full" and idle_seconds > self.idle_timeout_seconds)
        )

    def _resolve_defer_fold(
        self,
        utilization: float,
        idle_seconds: float,
        defer_fold: bool | Callable[[], bool] | None,
    ) -> bool:
        """把门输入解析成 bool；**只在真的逼近触发点时才调探针**。

        生产路径传下来的是零参 callable（Agent._has_in_progress_task 本身，不是它
        的返回值）：探针要读一次任务清单，而 check_and_compact 每次模型调用都跑，
        绝大多数时候利用率离任何触发点都很远、这一轮根本不做折叠决定。把解析放在
        这里，那次磁盘读就只发生在唯一用得着它的分支上（热路径上推式披露与常驻
        摘要已各读一次，不该无条件再加第三次）。

        本模块不 import task_store、也不给这个 callable 加任何语义：失败方向的裁定
        权在 Agent._has_in_progress_task 里（读不出来 → False → 照常折叠）。这里
        只认「一个返回 bool 的零参 callable，或者一个 bool」。
        """
        if not defer_fold:
            return False
        if not self._fold_trigger_in_reach(utilization, idle_seconds):
            return False
        return bool(defer_fold() if callable(defer_fold) else defer_fold)

    def _should_compress(self, utilization: float, idle_seconds: float,
                         defer_fold: bool | Callable[[], bool] = False) -> bool:
        """full 臂额外看空闲超时；其余臂只按利用率触发。

        defer_fold=True（有任务正在执行）时只认硬顶：任务执行中途，那些工具结果、
        文件内容、报错是模型正在推理的活工作集，折叠掉它们不是省钱而是破坏当前
        任务。空闲触发同样被压制——空闲折叠恰恰最容易落在任务中途。
        硬顶是安全阀：真的快撑爆窗口时，宁可破坏当前任务也不能让请求失败。

        defer_fold 也接受零参 callable（直接驱动本方法的调用方可以传探针本身）；
        经 run_pipeline 进来的一律已是解析好的 bool，见 _resolve_defer_fold。

        成本侧的理由见 deliverables/task-list-context-analysis.md §5 的实测：一次
        折叠使 87% 的旧消息需要重新预热，所以折叠次数越少越好，而「每任务最多
        一次」比「按阈值随机触发」少得多。
        """
        if callable(defer_fold):
            defer_fold = defer_fold()
        if defer_fold:
            return utilization > self._defer_ceiling()
        if self.arm == "full":
            return (
                utilization > self.tool_fold_threshold
                or idle_seconds > self.idle_timeout_seconds
            )
        return utilization > self.tool_fold_threshold

    def _tool_fold_sufficient(
        self,
        session: Any,
        current_token_count: int,
        message_tokens_before: int,
        span: Any,
    ) -> bool:
        """工具折叠后预估利用率是否已低于会话折叠阈值（是则免做会话级折叠）。"""
        estimated_tokens = self._estimate_tokens_after_fold(
            current_token_count,
            message_tokens_before,
            session,
        )
        estimated_utilization = estimated_tokens / self.effective_window if self.effective_window else 0
        if span:
            span.add_metadata(
                token_count_after_tool_fold=estimated_tokens,
                utilization_after_tool_fold=round(estimated_utilization, 3),
            )
        return estimated_utilization < self.session_fold_threshold

    async def _execute_compaction(
        self,
        session: Any,
        current_token_count: int,
        side_query: SideQueryFn | None,
        session_id: str,
        span: Any,
    ) -> bool:
        """按 arm 执行压缩：truncate → 直接截断；否则工具折叠，必要时会话折叠。"""
        hidden_before = _count_hidden_seqs(session)
        message_tokens_before = estimate_visible_message_tokens(session)

        if self.arm == "truncate":
            truncated = self._truncate_oldest_groups(
                session, current_token_count, message_tokens_before
            )
            if span:
                span.add_metadata(truncate=truncated)
            _finish_compaction_span(span, session, hidden_before, truncated, False)
            return truncated

        folded = False
        if self.arm == "session_only":
            if span:
                span.add_metadata(tool_fold=False)
        else:
            folded = await self._fold_tool_results(session, side_query)
            if span:
                span.add_metadata(tool_fold=folded)

        if self.arm == "tool_only":
            _finish_compaction_span(span, session, hidden_before, folded, False)
            return folded

        if folded and self._tool_fold_sufficient(
            session, current_token_count, message_tokens_before, span
        ):
            _finish_compaction_span(span, session, hidden_before, folded, False)
            return folded

        session_folded = await self._fold_session(
            session, side_query, session_id
        )
        folded = session_folded or folded
        if span:
            span.add_metadata(session_fold=session_folded)
        _finish_compaction_span(span, session, hidden_before, folded, session_folded)
        return folded

    async def run_pipeline(
        self,
        session: Any,
        current_token_count: int,
        last_api_call_time: float,
        side_query: SideQueryFn | None,
        session_id: str,
        defer_fold: bool | Callable[[], bool] | None = False,
    ) -> bool:
        """defer_fold: 任务边界折叠门（Plan 2 Task 4）的输入。

        由 Agent.check_and_compact 经 ContextManager._check_and_compact 传下来。
        生产路径上是 **Agent._has_in_progress_task 这个零参 callable 本身**，不是
        已经读好的 bool：探针要读一次任务清单，而 check_and_compact 每次模型调用
        都跑，所以解析推迟到 _resolve_defer_fold，只在逼近触发点时发生。也接受
        bool（直接驱动本方法的调用方与测试）。默认 False 保住既有调用方
        （含 test_compression_arms.py 的 5 个位置参数调用）的行为不变。
        """
        current_token_count = max(0, int(current_token_count))
        utilization = current_token_count / self.effective_window if self.effective_window else 0
        idle_seconds = time.time() - last_api_call_time if last_api_call_time else 0

        # 先解析成 bool 再往下走：print 与 trace metadata 都要可读的值，把 callable
        # 原样塞进去，事后只会看到一个 <bound method ...>，什么也判断不了。
        deferred = self._resolve_defer_fold(utilization, idle_seconds, defer_fold)
        should_compress = self._should_compress(utilization, idle_seconds, deferred)

        print(
            f"[compressor] check: arm={self.arm}, tokens={current_token_count}, utilization={utilization:.2%}, "
            f"idle={idle_seconds:.0f}s, threshold={self.tool_fold_threshold:.0%}, defer={deferred}, "
            f"should_compress={should_compress}"
        )

        if not should_compress:
            return False

        trigger = "utilization" if utilization > self.tool_fold_threshold else "idle"
        with trace_span(
            "compact",
            metadata={
                "trigger": trigger,
                "arm": self.arm,
                "token_count_before": current_token_count,
                "utilization_before": round(utilization, 3),
                "idle_seconds": round(idle_seconds, 1),
                "tool_fold_threshold": self.tool_fold_threshold,
                "session_fold_threshold": self.session_fold_threshold,
                # 事后能从 trace 里看出这次折叠发生时门是开还是关（Plan 2 Task 4）。
                # 记的是已解析的 bool；span 只在真的要折叠时创建，而折叠必然意味着
                # 触发点已在射程内，所以这里的值一定是探针的真实答案。
                "defer_fold": deferred,
            },
        ) as span:
            return await self._execute_compaction(
                session, current_token_count, side_query, session_id, span
            )

    def _truncate_oldest_groups(
        self,
        session: Any,
        current_token_count: int,
        message_tokens_before: int,
    ) -> bool:
        """truncate 臂：朴素硬截断，无任何摘要。

        与折叠臂共享同一触发点和目标水位（公平对比唯一变量=压缩机制）。
        从最旧消息组开始整组隐藏（保持 tool_call/tool_result 配对完整），
        直到估算 tokens 降到目标水位以下；首条用户消息（任务指令）永不截断，
        对齐滑动窗口截断基线的标准做法（system + 首轮指令 + 最近上下文）。
        """
        groups = build_message_groups(session)
        if not groups:
            return False

        overhead = max(0, current_token_count - message_tokens_before)
        target_tokens = max(
            0, int(self.effective_window * self.session_fold_threshold) - overhead
        )
        if message_tokens_before <= target_tokens:
            return False

        first_user_index = next(
            (index for index, group in enumerate(groups) if group.is_user), None
        )
        seqs_to_hide: list[int] = []
        remaining = message_tokens_before
        for index, group in enumerate(groups):
            if remaining <= target_tokens:
                break
            if index == first_user_index:
                continue
            seqs_to_hide.extend(group.seqs)
            remaining -= sum(estimate_event_tokens(event) for event in group.events)

        if not seqs_to_hide:
            return False
        session.hide_events(seqs_to_hide)
        self._truncation_count += 1
        self._record_fold_event()
        return True

    def _estimate_tokens_after_fold(
        self,
        current_token_count: int,
        message_tokens_before: int,
        session: Any,
    ) -> int:
        message_tokens_after = estimate_visible_message_tokens(session)
        fixed_tokens = max(0, current_token_count - message_tokens_before)
        return fixed_tokens + message_tokens_after

    async def compact_manual(
        self,
        session: Any,
        side_query: SideQueryFn | None,
        session_id: str,
    ) -> bool:
        """手动触发会话折叠。"""
        if len(session.events) < 4:
            return False


        with trace_span("compact", metadata={"trigger": "manual"}) as span:
            hidden_before = _count_hidden_seqs(session)
            folded = await self._fold_session(session, side_query, session_id, trigger="manual")
            _finish_compaction_span(span, session, hidden_before, folded, folded)
            return folded

    async def _fold_tool_results(self, session: Any, side_query: SideQueryFn | None) -> bool:
        groups = build_message_groups(session)
        fold_indices = find_tool_fold_indices(groups, self.keep_recent_tool_rounds)
        if not fold_indices:
            return False

        abstracts_by_key: dict[str, dict[str, Any]] = {}
        assistant_texts_by_seq: dict[int, dict[str, Any]] = {}
        pending_results: list[dict[str, Any]] = []
        seqs_to_hide: list[int] = []

        for index in fold_indices:
            group = groups[index]
            call_index = self._tool_call_index(group)

            for event in group.events:
                event_type = event.get("type")
                seq = event.get("seq")
                if event_type == "tool_folded":
                    self._merge_tool_folded_event(
                        event,
                        abstracts_by_key,
                        assistant_texts_by_seq,
                    )
                elif event_type == "assistant_message":
                    text = str(event.get("content") or "").strip()
                    if text and isinstance(seq, int):
                        assistant_texts_by_seq[seq] = {
                            "seq": seq,
                            "text": clip_text(text, self.assistant_text_char_limit),
                        }
                elif event_type == "tool_result_msg":
                    call_id = str(event.get("call_id") or "")
                    call = call_index.get(call_id, {})
                    pending_results.append({
                        "key": call_id or f"__seq_{seq}",
                        "seq": seq if isinstance(seq, int) else None,
                        "call_id": call_id,
                        "tool_name": call.get("tool_name", ""),
                        "arguments": call.get("arguments", ""),
                        "content": str(event.get("content") or ""),
                    })

            seqs_to_hide.extend(group.seqs)

        seqs_to_hide = list(dict.fromkeys(seqs_to_hide))
        if pending_results:
            for abstract in await self._make_tool_abstracts(pending_results, side_query):
                key = str(abstract.get("call_id") or "") or f"__seq_{abstract.get('seq')}"
                abstracts_by_key[key] = abstract

        abstracts = self._sort_fold_entries(abstracts_by_key.values())
        assistant_texts = self._sort_fold_entries(assistant_texts_by_seq.values())
        if not seqs_to_hide or (not abstracts and not assistant_texts):
            return False

        session.hide_events(seqs_to_hide)
        session.append("tool_folded", {
            "abstracts": abstracts,
            "assistant_texts": assistant_texts,
            "trigger": "auto",
        })
        self._tool_fold_count += 1
        self._record_fold_event()
        return True

    def _merge_tool_folded_event(
        self,
        event: dict[str, Any],
        abstracts_by_key: dict[str, dict[str, Any]],
        assistant_texts_by_seq: dict[int, dict[str, Any]],
    ) -> None:
        for abstract in event.get("abstracts", []):
            if not isinstance(abstract, dict):
                continue
            call_id = str(abstract.get("call_id") or "")
            key = call_id or f"__seq_{abstract.get('seq')}"
            abstracts_by_key[key] = abstract

        for text_entry in event.get("assistant_texts", []):
            if not isinstance(text_entry, dict):
                continue
            seq = text_entry.get("seq")
            if isinstance(seq, int):
                assistant_texts_by_seq[seq] = {
                    "seq": seq,
                    "text": str(text_entry.get("text") or ""),
                }

    def _sort_fold_entries(self, entries: Any) -> list[dict[str, Any]]:
        items = [entry for entry in entries if isinstance(entry, dict)]

        def sort_key(item: tuple[int, dict[str, Any]]) -> tuple[int, int, int]:
            index, entry = item
            seq = entry.get("seq")
            if isinstance(seq, int):
                return (0, seq, index)
            return (1, index, index)

        return [entry for _, entry in sorted(enumerate(items), key=sort_key)]

    def _tool_call_index(self, group: Any) -> dict[str, dict[str, str]]:
        index: dict[str, dict[str, str]] = {}
        for event in group.events:
            if event.get("type") != "assistant_message":
                continue
            for tool_call in event.get("tool_calls") or []:
                if not isinstance(tool_call, dict):
                    continue
                call_id = str(tool_call.get("id") or "")
                if not call_id:
                    continue
                function = tool_call.get("function") or {}
                index[call_id] = {
                    "tool_name": str(function.get("name") or ""),
                    "arguments": str(function.get("arguments") or ""),
                }
        return index

    async def _make_tool_abstracts(
        self,
        items: list[dict[str, Any]],
        side_query: SideQueryFn | None,
    ) -> list[dict[str, Any]]:
        abstracts: list[dict[str, Any]] = []
        candidates: list[dict[str, Any]] = []

        for item in items:
            content = str(item.get("content") or "")
            abstract = {
                "seq": item.get("seq"),
                "call_id": item.get("call_id", ""),
                "tool_name": item.get("tool_name", ""),
                "arguments": clip_text(str(item.get("arguments") or ""), 500),
                "abstract": clip_text(content, self.tool_abstract_char_limit),
            }
            abstracts.append(abstract)
            if len(content) > self.tool_abstract_char_limit:
                candidates.append({
                    "call_id": abstract["call_id"] or f"__seq_{abstract['seq']}",
                    "tool_name": abstract["tool_name"],
                    "arguments": abstract["arguments"],
                    "content": clip_text(content, self.tool_abstract_input_char_limit),
                })

        if not side_query or not candidates:
            return abstracts

        candidates.sort(key=lambda candidate: len(candidate["content"]), reverse=True)
        selected_candidates: list[dict[str, Any]] = []
        remaining_budget = self.tool_abstract_batch_char_limit
        for candidate in candidates:
            if remaining_budget <= 0:
                break
            content = candidate["content"]
            if len(content) > remaining_budget:
                content = content[:remaining_budget] + "\n[... batch input truncated ...]"
            selected_candidates.append({**candidate, "content": content})
            remaining_budget -= len(content)

        try:
            raw = await side_query(
                TOOL_ABSTRACT_SYSTEM,
                "Compress these tool results:\n"
                + json.dumps(selected_candidates, ensure_ascii=False),
            )
            summarized = self._parse_tool_abstracts(raw)
        except Exception as exc:
            logger.warning("[tool_fold] batch side query failed: %s: %s", type(exc).__name__, exc)
            return abstracts

        candidate_keys = {candidate["call_id"] for candidate in selected_candidates}
        for abstract in abstracts:
            key = abstract["call_id"] or f"__seq_{abstract['seq']}"
            text = summarized.get(key)
            if key in candidate_keys and text:
                abstract["abstract"] = clip_text(text, self.tool_abstract_char_limit)
        return abstracts

    def _parse_tool_abstracts(self, raw: str) -> dict[str, str]:
        text = str(raw or "").strip()
        fenced = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text)
        if fenced:
            text = fenced.group(1).strip()
        obj = re.search(r"\{[\s\S]*\}", text)
        if obj:
            text = obj.group(0).strip()

        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            logger.warning("[tool_fold] abstract response is not valid JSON: %s", exc)
            return {}

        if not isinstance(parsed, dict):
            return {}

        result: dict[str, str] = {}
        for item in parsed.get("abstracts", []):
            if not isinstance(item, dict):
                continue
            call_id = str(item.get("call_id") or "")
            abstract = str(item.get("abstract") or "").strip()
            if call_id and abstract:
                result[call_id] = abstract
        return result

    async def _resolve_fold_notes(
        self,
        side_query: SideQueryFn | None,
        transcript: str,
        previous_notes: str,
        fallback_transcript: str,
    ) -> tuple[str, str, str, bool]:
        """有 side_query 走 LLM 编译会话笔记；否则用回退格式化。返回 (summary, notes, knowledge, validated)。"""
        if side_query:
            return await self._compile_session_notes(
                side_query, transcript, previous_notes, fallback_transcript
            )
        summary = format_folded_memory(fallback_folded_memory(fallback_transcript))
        return summary, "", "", False

    def _spawn_wiki_fold_task(
        self,
        session: Any,
        session_id: str,
        session_notes: str,
        side_query: SideQueryFn | None,
    ) -> None:
        """会话折叠后异步触发 Wiki 编译（fire-and-forget，任务持有防 GC）。"""
        workspace = _current_workspace.get()
        task = asyncio.create_task(
            on_session_folded(
                session_id=session_id,
                session=session,
                session_notes=session_notes,
                side_query=side_query,
                workspace=workspace,
            )
        )
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def _fold_session(
        self,
        session: Any,
        side_query: SideQueryFn | None,
        session_id: str,
        trigger: str = "auto",
    ) -> bool:
        groups = build_message_groups(session)
        tail_token_budget = min(
            CHECKPOINT_TAIL_TOKEN_BUDGET,
            int(self.effective_window * CHECKPOINT_TAIL_WINDOW_RATIO),
        )
        cut_index = find_session_fold_cut(
            groups,
            self.keep_recent_dialog_rounds,
            self.keep_recent_trajectory_tool_rounds,
            tail_token_budget=tail_token_budget,
        )
        if cut_index <= 0:
            return False

        folded_groups = groups[:cut_index]
        user_group_count = sum(1 for group in groups if group.is_user)
        fold_mode = "dialog" if user_group_count > self.keep_recent_dialog_rounds else "trajectory"
        previous_notes = latest_session_notes(session)
        transcript_groups = [group for group in folded_groups if group.kind != "session_folded"]
        transcript = build_groups_transcript(transcript_groups)

        if not transcript.strip() and not previous_notes.strip():
            return False

        fallback_transcript = transcript
        if previous_notes:
            fallback_transcript = (
                f"Previous session notes:\n{previous_notes}\n\n"
                f"New conversation transcript:\n{transcript}"
            )

        summary, session_notes, project_knowledge, notes_validated = (
            await self._resolve_fold_notes(
                side_query, transcript, previous_notes, fallback_transcript
            )
        )

        # trajectory 模式保留用户组（任务锚点），dialog 模式全部隐藏；去重保序
        seqs_to_hide: list[int] = []
        for group in folded_groups:
            if fold_mode == "trajectory" and group.is_user:
                continue
            seqs_to_hide.extend(group.seqs)
        seqs_to_hide = list(dict.fromkeys(seqs_to_hide))
        if not seqs_to_hide:
            return False

        session.hide_events(seqs_to_hide)
        session.append("session_folded", {
            "summary": summary,
            "session_notes": session_notes,
            "project_knowledge": project_knowledge,
            "notes_validated": notes_validated,
            "previous_notes_chars": len(previous_notes),
            "transcript_chars": len(transcript),
            "checkpoint_tail_tokens": sum(
                estimate_group_tokens(group) for group in groups[cut_index:]
            ),
            "fold_mode": fold_mode,
            "trigger": trigger,
        })

        if self.wiki_enabled:
            self._spawn_wiki_fold_task(session, session_id, session_notes, side_query)

        self._session_fold_count += 1
        self._record_fold_event()
        return True

    async def _compile_session_notes(
        self,
        side_query: SideQueryFn,
        transcript: str,
        previous_notes: str,
        fallback_transcript: str,
    ) -> tuple[str, str, str, bool]:
        """会话笔记编译：结构校验 + 纠正重试一次 + 显式失败审计。

        返回 (summary, session_notes, project_knowledge, notes_validated)。
        对齐 v2 compaction.ts：模板命中检查失败 → 追加纠正提示重试一次 →
        仍失败显式报错（logger.error + session_folded.notes_validated=False），
        不静默吞异常。notes 非空但结构不合格时仍保留内容（内容优先于格式），
        只有彻底失败（异常/空输出）才退回确定性 fallback 摘要。
        """
        compile_prompt = self._build_compile_prompt(transcript, previous_notes)
        notes = ""
        knowledge = ""
        validated = False
        try:
            raw = await side_query(COMPILE_SESSION_NOTES_AND_KNOWLEDGE_SYSTEM, compile_prompt)
            print(f"[side_query] raw response length={len(raw) if raw else 0}")
            compiled = self._parse_compiled_result(raw)
            notes = compiled.get("session_notes", "")
            knowledge = compiled.get("project_knowledge", "")
            print(
                f"[side_query] parsed: session_notes={len(notes)}chars, "
                f"project_knowledge={len(knowledge)}chars"
            )

            missing = validate_session_notes(notes) if notes else list(REQUIRED_NOTES_SECTIONS)
            if missing:
                logger.warning(
                    "[side_query] session_notes missing sections %s, retrying once", missing
                )
                retry_prompt = (
                    compile_prompt
                    + "\n\nYour previous output was missing required sections: "
                    + ", ".join(missing)
                    + "\nRegenerate session_notes containing ALL required ## section headings verbatim, in order."
                    + "\n\nPrevious output:\n"
                    + clip_text(notes or str(raw or ""), 8000)
                )
                raw2 = await side_query(COMPILE_SESSION_NOTES_AND_KNOWLEDGE_SYSTEM, retry_prompt)
                compiled2 = self._parse_compiled_result(raw2)
                notes2 = compiled2.get("session_notes", "")
                if notes2 and len(validate_session_notes(notes2)) < len(missing):
                    notes = notes2
                    knowledge = compiled2.get("project_knowledge", "") or knowledge
                    missing = validate_session_notes(notes)
            validated = bool(notes) and not missing
            if not validated:
                logger.error(
                    "[side_query] session_notes structure validation failed after retry "
                    "(missing=%s, notes_chars=%d)",
                    missing,
                    len(notes),
                )
        except Exception as e:
            logger.error("[side_query] failed: %s: %s", type(e).__name__, e)
            notes = ""

        if notes:
            summary = self._format_session_notes_as_summary(notes)
        else:
            summary = format_folded_memory(fallback_folded_memory(fallback_transcript))
        return summary, notes, knowledge, validated

    def _build_compile_prompt(self, transcript: str, previous_notes: str = "") -> str:
        sections: list[str] = []
        if previous_notes.strip():
            sections.append(
                "Previous session notes (merge and update them, do not duplicate):\n"
                f"{clip_text(previous_notes, 20000)}"
            )
        sections.append(
            "New conversation transcript:\n"
            f"{transcript or '(empty)'}"
        )
        sections.append(
            "Return one updated session_notes value that merges the previous notes and the new transcript. "
            "Keep still-relevant goals, constraints, progress, decisions, failures, and next actions. "
            "Preserve exact identifiers verbatim (sub-agent session_id from <subagent ...> tags, file paths, URLs, job IDs) — they are required to resume work. "
            "Remove information that has been superseded, completed with no continuing relevance, or proven wrong."
        )
        return "\n\n".join(sections)


    def _parse_compiled_result(self, raw: str) -> dict[str, str]:
        text = str(raw or "").strip()
        fenced = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text)
        if fenced:
            text = fenced.group(1).strip()
        obj = re.search(r"\{[\s\S]*\}", text)
        if obj:
            text = obj.group(0).strip()
        
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                return {
                    "session_notes": str(parsed.get("session_notes", "")),
                    "project_knowledge": str(parsed.get("project_knowledge", "")),
                }
        except json.JSONDecodeError as exc:
            logger.warning("[side_query] compiled result is not valid JSON: %s", exc)

        return {
            "session_notes": text,
            "project_knowledge": "",
        }

    def _format_session_notes_as_summary(self, session_notes: str) -> str:
        """将会话笔记格式化为摘要，注入新上下文。"""
        return (
            "<session-folded-memory>\n"
            "Previous raw conversation history was compacted. Use these session notes as session state, "
            "but verify file contents and live environment state before making code changes.\n\n"
            f"{session_notes}\n"
            "</session-folded-memory>\n\n"
            "Continue the task from this state."
        )

    def _record_fold_event(self) -> None:
        self._fold_last_time = time.time()
        self._fold_count += 1

    @property
    def last_fold_time_iso(self) -> str | None:
        if self._fold_last_time <= 0:
            return None
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self._fold_last_time))

    def get_stats(self) -> dict[str, Any]:
        return {
            "tool_fold": {
                "triggered": self._tool_fold_count,
            },
            "session_fold": {
                "triggered": self._session_fold_count,
            },
            "truncate": {
                "triggered": self._truncation_count,
            },
            "total_folds": {
                "triggered": self._fold_count,
                "last_fold_time": self.last_fold_time_iso,
            },
        }
