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
    estimate_visible_message_tokens,
    find_session_fold_cut,
    find_tool_fold_indices,
    latest_session_notes,
)
from .session_memory import (
    clip_text,
    fallback_folded_memory,
    format_folded_memory,
)
from .session import save_folded_session_memory

logger = logging.getLogger(__name__)

SESSION_FOLD_THRESHOLD = 0.40
# 压缩对比实验臂（GAIA 消融）：full=现状；none=完全不压缩（配合 1M 窗口）；
# tool_only=仅工具结果折叠；session_only=仅会话折叠。实验臂只保留利用率触发，
# 禁用 idle 触发——idle 折叠会连带执行另一种机制，污染单臂对比。
COMPRESSION_ARMS = {"full", "none", "tool_only", "session_only"}
KEEP_RECENT_TOOL_ROUNDS = 3
KEEP_RECENT_DIALOG_ROUNDS = 2
KEEP_RECENT_TRAJECTORY_TOOL_ROUNDS = 5
IDLE_TIMEOUT_S = 5 * 60
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
- Keep each abstract concise and under 1200 characters.
- Do not invent details that are absent from the tool result.
"""

# 会话笔记和项目知识编译的 system prompt - 从文件加载
_PROMPTS_DIR = Path(__file__).parent.parent / "prompts" / "side_query"
COMPILE_SESSION_NOTES_AND_KNOWLEDGE_SYSTEM = (_PROMPTS_DIR / "compile_session.txt").read_text(encoding="utf-8")


def _sanitize_for_utf8(value: Any) -> Any:
    if isinstance(value, str):
        return value.encode("utf-8", errors="replace").decode("utf-8")
    if isinstance(value, list):
        return [_sanitize_for_utf8(v) for v in value]
    if isinstance(value, dict):
        return {k: _sanitize_for_utf8(v) for k, v in value.items()}
    return value


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
        self._background_tasks: set[asyncio.Task[Any]] = set()

    async def run_pipeline(
        self,
        session: Any,
        current_token_count: int,
        last_api_call_time: float,
        side_query: SideQueryFn | None,
        session_id: str,
        folded_memories: list[dict],
    ) -> bool:
        if self.arm == "none":
            return False

        current_token_count = max(0, int(current_token_count))
        utilization = current_token_count / self.effective_window if self.effective_window else 0
        idle_seconds = time.time() - last_api_call_time if last_api_call_time else 0

        folded = False
        if self.arm == "full":
            should_compress = (
                utilization > self.tool_fold_threshold
                or idle_seconds > self.idle_timeout_seconds
            )
        else:
            should_compress = utilization > self.tool_fold_threshold

        print(
            f"[compressor] check: arm={self.arm}, tokens={current_token_count}, utilization={utilization:.2%}, "
            f"idle={idle_seconds:.0f}s, threshold={self.tool_fold_threshold:.0%}, should_compress={should_compress}"
        )

        if not should_compress:
            return False

        from agents.observability.trace import trace_span

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
            },
        ) as span:
            hidden_before = _count_hidden_seqs(session)
            message_tokens_before = estimate_visible_message_tokens(session)

            if self.arm == "session_only":
                folded = False
                if span:
                    span.add_metadata(tool_fold=False)
            else:
                folded = await self._fold_tool_results(session, side_query)
                if span:
                    span.add_metadata(tool_fold=folded)

            if self.arm == "tool_only":
                _finish_compaction_span(span, session, hidden_before, folded, False)
                return folded

            if folded:
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
                if estimated_utilization < self.session_fold_threshold:
                    _finish_compaction_span(span, session, hidden_before, folded, False)
                    return folded

            session_folded = await self._fold_session(
                session, side_query, session_id, folded_memories
            )
            folded = session_folded or folded
            if span:
                span.add_metadata(session_fold=session_folded)
            _finish_compaction_span(span, session, hidden_before, folded, session_folded)

        return folded

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
        folded_memories: list[dict],
    ) -> bool:
        """手动触发会话折叠。"""
        if len(session.events) < 4:
            return False

        from agents.observability.trace import trace_span

        with trace_span("compact", metadata={"trigger": "manual"}) as span:
            hidden_before = _count_hidden_seqs(session)
            folded = await self._fold_session(session, side_query, session_id, folded_memories, trigger="manual")
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

    async def _fold_session(
        self,
        session: Any,
        side_query: SideQueryFn | None,
        session_id: str,
        folded_memories: list[dict],
        trigger: str = "auto",
    ) -> bool:
        groups = build_message_groups(session)
        cut_index = find_session_fold_cut(
            groups,
            self.keep_recent_dialog_rounds,
            self.keep_recent_trajectory_tool_rounds,
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

        session_notes = ""
        project_knowledge = ""
        summary = ""

        if side_query:
            try:
                raw = await side_query(
                    COMPILE_SESSION_NOTES_AND_KNOWLEDGE_SYSTEM,
                    self._build_compile_prompt(transcript, previous_notes),
                )
                print(f"[side_query] raw response length={len(raw) if raw else 0}")
                compiled = self._parse_compiled_result(raw)
                session_notes = compiled.get("session_notes", "")
                project_knowledge = compiled.get("project_knowledge", "")
                print(
                    f"[side_query] parsed: session_notes={len(session_notes)}chars, "
                    f"project_knowledge={len(project_knowledge)}chars"
                )

                if session_notes:
                    summary = self._format_session_notes_as_summary(session_notes)
                else:
                    summary = format_folded_memory(fallback_folded_memory(fallback_transcript))
            except Exception as e:
                logger.error("[side_query] failed: %s: %s", type(e).__name__, e)
                summary = format_folded_memory(fallback_folded_memory(fallback_transcript))
        else:
            summary = format_folded_memory(fallback_folded_memory(fallback_transcript))

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
            "previous_notes_chars": len(previous_notes),
            "transcript_chars": len(transcript),
            "fold_mode": fold_mode,
            "trigger": trigger,
        })

        record = {
            "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "trigger": trigger,
            "fold_mode": fold_mode,
            "session_id": session_id,
            "summary": summary,
            "session_notes": session_notes,
            "project_knowledge": project_knowledge,
        }
        folded_memories.append(record)
        try:
            await asyncio.to_thread(save_folded_session_memory, session_id, _sanitize_for_utf8(record))
        except Exception as exc:
            logger.error("[session_fold] failed to persist folded memory: %s: %s", type(exc).__name__, exc)

        if self.wiki_enabled:
            from agents.core.workspace import _current_workspace
            from agents.wiki.pipeline import on_session_folded
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

        self._session_fold_count += 1
        self._record_fold_event()
        return True

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
            "total_folds": {
                "triggered": self._fold_count,
                "last_fold_time": self.last_fold_time_iso,
            },
        }
