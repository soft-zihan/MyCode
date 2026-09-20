"""ContextCompressor — 两级上下文压缩管线。

职责：
- 策略 A：工具调用折叠（上下文 > 80% 或空闲 > 5 分钟）
- 策略 B：会话折叠（工具折叠后仍 >= 40%）

折叠通过追加 events_hidden 事件实现，原始内容保留在事件中。
摘要作为独立事件（tool_folded / session_folded）插入。

会话折叠时，一次 side query 同时编译任务笔记和项目知识。
任务笔记立即注入新上下文，项目知识异步写入 wiki。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any, Callable, Awaitable

from .session_memory import (
    fallback_folded_memory,
    format_folded_memory,
    FOLD_SESSION_MEMORY_SYSTEM,
)
from .session import save_folded_session_memory
from agents.logging import print_info

logger = logging.getLogger(__name__)

TOOL_FOLD_THRESHOLD = 0.80
SESSION_FOLD_THRESHOLD = 0.40
KEEP_RECENT_TOOL_ROUNDS = 3
KEEP_RECENT_DIALOG_ROUNDS = 2
IDLE_TIMEOUT_S = 5 * 60
SINGLE_RESULT_CHAR_LIMIT = 30000

SideQueryFn = Callable[[str, str], Awaitable[str]]

# 任务笔记和项目知识编译的 system prompt - 从文件加载
_PROMPTS_DIR = Path(__file__).parent.parent / "prompts" / "side_query"
COMPILE_TASK_NOTES_AND_KNOWLEDGE_SYSTEM = (_PROMPTS_DIR / "compile_session.txt").read_text(encoding="utf-8")


def _sanitize_for_utf8(value: Any) -> Any:
    if isinstance(value, str):
        return value.encode("utf-8", errors="replace").decode("utf-8")
    if isinstance(value, list):
        return [_sanitize_for_utf8(v) for v in value]
    if isinstance(value, dict):
        return {k: _sanitize_for_utf8(v) for k, v in value.items()}
    return value


def _count_hidden_seqs(session: Any) -> int:
    """统计当前 events_hidden 累计隐藏的 seq 数。"""
    hidden: set = set()
    for event in session._log:
        if event.get("type") == "events_hidden":
            hidden.update(event.get("hidden_seqs", []))
    return len(hidden)


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
    total = len(session._log)
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
        tool_fold_threshold: float = TOOL_FOLD_THRESHOLD,
        session_fold_threshold: float = SESSION_FOLD_THRESHOLD,
        keep_recent_tool_rounds: int = KEEP_RECENT_TOOL_ROUNDS,
        keep_recent_dialog_rounds: int = KEEP_RECENT_DIALOG_ROUNDS,
        idle_timeout_seconds: int = IDLE_TIMEOUT_S,
        single_result_char_limit: int = SINGLE_RESULT_CHAR_LIMIT,
        effective_window: int = 108800,
    ) -> None:
        self.tool_fold_threshold = tool_fold_threshold
        self.session_fold_threshold = session_fold_threshold
        self.keep_recent_tool_rounds = keep_recent_tool_rounds
        self.keep_recent_dialog_rounds = keep_recent_dialog_rounds
        self.idle_timeout_seconds = idle_timeout_seconds
        self.single_result_char_limit = single_result_char_limit
        self.effective_window = effective_window

        self._fold_last_time: float = 0.0
        self._fold_count: int = 0
        self._tool_fold_count: int = 0
        self._session_fold_count: int = 0

    async def run_pipeline(
        self,
        session: Any,
        last_input_token_count: int,
        last_api_call_time: float,
        side_query: SideQueryFn | None,
        session_id: str,
        folded_memories: list[dict],
    ) -> bool:
        """主入口：根据条件选择压缩策略。
        
        新流程：
        1. >80% 或空闲 >5 分钟：开始压缩
        2. 先做工具折叠（可恢复）
        3. 估算折叠后 token 数，<40% 则停止
        4. 否则做会话折叠（side query 同时编译任务笔记和项目知识）
        
        Returns:
            bool: 是否执行了压缩
        """
        utilization = last_input_token_count / self.effective_window if self.effective_window else 0
        idle_seconds = time.time() - last_api_call_time if last_api_call_time else 0

        folded = False

        # 触发条件：>80% 或空闲 >5 分钟
        should_compress = (
            utilization > self.tool_fold_threshold
            or idle_seconds > self.idle_timeout_seconds
        )

        print(f"[compressor] check: utilization={utilization:.2%}, idle={idle_seconds:.0f}s, threshold={self.tool_fold_threshold:.0%}, should_compress={should_compress}")

        if not should_compress:
            return False

        from agents.observability.trace import trace_span

        trigger = "utilization" if utilization > self.tool_fold_threshold else "idle"
        with trace_span(
            "compact",
            metadata={
                "trigger": trigger,
                "utilization_before": round(utilization, 3),
                "idle_seconds": round(idle_seconds, 1),
            },
        ) as span:
            hidden_before = _count_hidden_seqs(session)

            # 第一步：工具折叠（可恢复）
            folded = await self._fold_tool_results(session, side_query)
            if span:
                span.add_metadata(tool_fold=folded)

            # 第二步：估算折叠后 token 数，<40% 则停止（跳过会话折叠）
            if folded and side_query:
                estimated_utilization = self._estimate_utilization_after_tool_fold(
                    session, utilization
                )
                if span:
                    span.add_metadata(utilization_after_tool_fold=round(estimated_utilization, 3))
                if estimated_utilization < self.session_fold_threshold:
                    _finish_compaction_span(span, session, hidden_before, folded, False)
                    return folded

            # 第三步：会话折叠（side query 同时编译任务笔记和项目知识）
            session_folded = await self._fold_session(
                session, side_query, session_id, folded_memories
            )
            folded = session_folded or folded
            if span:
                span.add_metadata(session_fold=session_folded)
            _finish_compaction_span(span, session, hidden_before, folded, session_folded)

        return folded

    def _estimate_utilization_after_tool_fold(
        self, session: Any, current_utilization: float
    ) -> float:
        """估算工具折叠后的 token 利用率。
        
        简化启发式：假设工具结果占总 token 的比例与工具结果消息占比成正比。
        """
        # 收集所有隐藏的 seq
        hidden_seqs = set()
        for event in session._log:
            if event.get("type") == "events_hidden":
                hidden_seqs.update(event.get("hidden_seqs", []))
        
        # 统计可见消息中工具结果的比例
        tool_chars = 0
        total_chars = 0
        for event in session._log:
            seq = event.get("seq")
            if seq in hidden_seqs:
                continue
            t = event.get("type")
            if t in ("user_message", "assistant_message", "tool_result_msg"):
                content = event.get("content", "")
                content_len = len(content) if isinstance(content, str) else len(str(content))
                total_chars += content_len
                if t == "tool_result_msg":
                    tool_chars += content_len
        
        if total_chars == 0:
            return current_utilization
        
        # 假设工具结果占总 token 的比例与字符比例成正比
        tool_ratio = tool_chars / total_chars
        # 估算折叠后的利用率
        estimated_utilization = current_utilization * (1 - tool_ratio * 0.7)
        return estimated_utilization

    async def compact_manual(
        self,
        session: Any,
        side_query: SideQueryFn | None,
        session_id: str,
        folded_memories: list[dict],
    ) -> bool:
        """手动触发会话折叠。"""
        if len(session._log) < 4:
            return False

        from agents.observability.trace import trace_span

        with trace_span("compact", metadata={"trigger": "manual"}) as span:
            hidden_before = _count_hidden_seqs(session)
            folded = await self._fold_session(session, side_query, session_id, folded_memories)
            _finish_compaction_span(span, session, hidden_before, folded, folded)
            return folded

    async def _fold_tool_results(self, session: Any, side_query: SideQueryFn | None) -> bool:
        """策略 A：工具调用折叠。
        
        保留最近 N 轮工具调用，更早的追加 events_hidden 事件。
        摘要写入 tool_folded 事件。长结果直接截断内容。
        """
        # 收集所有隐藏的 seq
        hidden_seqs = set()
        for event in session._log:
            if event.get("type") == "events_hidden":
                hidden_seqs.update(event.get("hidden_seqs", []))
        
        # 找到所有可见的工具结果事件
        tool_events = [
            e for e in session._log
            if e.get("type") == "tool_result_msg" and e.get("seq") not in hidden_seqs
        ]
        
        if len(tool_events) <= self.keep_recent_tool_rounds:
            return False
        
        to_fold = tool_events[:-self.keep_recent_tool_rounds]
        hidden_seqs_to_hide = []
        abstracts = []
        
        for event in to_fold:
            content = event.get("content", "")
            call_id = event.get("call_id", "")
            
            if len(content) > self.single_result_char_limit:
                if side_query:
                    try:
                        abstract = await side_query(
                            "Summarize this tool result concisely:",
                            content[:50000]
                        )
                    except Exception:
                        abstract = None
                    if not abstract:
                        keep = (self.single_result_char_limit - 80) // 2
                        abstract = content[:keep] + "\n[... truncated ...]\n" + content[-keep:]
                        event["content"] = abstract
                    else:
                        event["content"] = abstract
                else:
                    keep = (self.single_result_char_limit - 80) // 2
                    abstract = content[:keep] + "\n[... truncated ...]\n" + content[-keep:]
                    event["content"] = abstract
            else:
                abstract = content
            
            hidden_seqs_to_hide.append(event["seq"])
            abstracts.append({"call_id": call_id, "abstract": abstract})
        
        if hidden_seqs_to_hide:
            session.hide_events(hidden_seqs_to_hide)
            session.append("tool_folded", {
                "abstracts": abstracts,
                "trigger": "auto",
            })
            self._tool_fold_count += 1
            self._record_fold_event()
            return True
        
        return False

    async def _fold_session(
        self,
        session: Any,
        side_query: SideQueryFn | None,
        session_id: str,
        folded_memories: list[dict],
    ) -> bool:
        """策略 B：会话折叠。
        
        保留最近 N 轮对话，更早的追加 events_hidden 事件。
        一次 side query 同时编译任务笔记和项目知识。
        任务笔记立即注入新上下文，项目知识异步写入 wiki。
        """
        # 先做工具折叠
        await self._fold_tool_results(session, side_query)
        
        # 收集所有隐藏的 seq
        hidden_seqs = set()
        for event in session._log:
            if event.get("type") == "events_hidden":
                hidden_seqs.update(event.get("hidden_seqs", []))
        
        # 找到所有可见的对话事件
        dialog_events = [
            e for e in session._log
            if e.get("type") in ("user_message", "assistant_message", "tool_result_msg")
            and e.get("seq") not in hidden_seqs
        ]
        
        keep_count = self.keep_recent_dialog_rounds * 3
        if len(dialog_events) <= keep_count:
            return False
        
        to_fold = dialog_events[:-keep_count]
        
        transcript = self._build_transcript(to_fold)
        if not transcript.strip():
            return False
        
        # 一次 side query 同时编译会话笔记和项目知识
        session_notes = ""
        project_knowledge = ""
        summary = ""
        
        if side_query:
            try:
                raw = await side_query(
                    COMPILE_TASK_NOTES_AND_KNOWLEDGE_SYSTEM,
                    self._build_compile_prompt(transcript)
                )
                print(f"[side_query] raw response length={len(raw) if raw else 0}")
                compiled = self._parse_compiled_result(raw)
                session_notes = compiled.get("session_notes", "")
                project_knowledge = compiled.get("project_knowledge", "")
                print(f"[side_query] parsed: session_notes={len(session_notes)}chars, project_knowledge={len(project_knowledge)}chars")
                
                # 会话笔记作为摘要注入新上下文
                if session_notes:
                    summary = self._format_session_notes_as_summary(session_notes)
                else:
                    summary = format_folded_memory(fallback_folded_memory(transcript))
            except Exception as e:
                logger.error(f"[side_query] failed: {type(e).__name__}: {e}")
                summary = format_folded_memory(fallback_folded_memory(transcript))
        else:
            summary = format_folded_memory(fallback_folded_memory(transcript))
        
        hidden_seqs_to_hide = [e["seq"] for e in to_fold]
        session.hide_events(hidden_seqs_to_hide)
        
        session.append("session_folded", {
            "summary": summary,
            "session_notes": session_notes,
            "project_knowledge": project_knowledge,
            "trigger": "auto",
        })
        
        # 保存折叠记忆
        try:
            record = {
                "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "trigger": "auto",
                "session_id": session_id,
                "summary": summary,
                "session_notes": session_notes,
                "project_knowledge": project_knowledge,
            }
            folded_memories.append(record)
            await asyncio.to_thread(save_folded_session_memory, session_id, _sanitize_for_utf8(record))
        except Exception:
            pass
        
        # 异步写入 wiki
        if session_notes or project_knowledge:
            # 传播 contextvar 到异步任务
            from agents.core.workspace import _current_workspace
            workspace = _current_workspace.get()
            asyncio.create_task(
                self._write_wiki_async(session_id, session_notes, project_knowledge, workspace)
            )
        
        self._session_fold_count += 1
        self._record_fold_event()
        return True

    def _build_compile_prompt(self, transcript: str) -> str:
        """构建编译会话笔记和项目知识的 prompt。"""
        return (
            "Extract session notes and project knowledge from the following conversation history.\n\n"
            "Conversation transcript:\n"
            f"{transcript}"
        )

    def _parse_compiled_result(self, raw: str) -> dict[str, str]:
        """解析 side query 返回的 JSON 结果。"""
        import re
        text = str(raw or "").strip()
        # 尝试提取 JSON
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
        except Exception:
            pass
        
        # 如果解析失败，将整个内容作为会话笔记
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

    async def _write_wiki_async(
        self,
        session_id: str,
        session_notes: str,
        project_knowledge: str,
        workspace: Path | None = None,
    ) -> None:
        """异步写入 wiki，写入前预检索避免重复。"""
        # 设置 contextvar
        if workspace is not None:
            from agents.core.workspace import set_workspace, reset_workspace
            token = set_workspace(workspace)
            try:
                await self._do_write_wiki(session_id, session_notes, project_knowledge)
            finally:
                reset_workspace(token)
        else:
            await self._do_write_wiki(session_id, session_notes, project_knowledge)
    
    async def _do_write_wiki(
        self,
        session_id: str,
        session_notes: str,
        project_knowledge: str,
    ) -> None:
        """实际执行 wiki 写入。"""
        print(f"[wiki_write] start session={session_id} session_notes={len(session_notes)}chars knowledge={len(project_knowledge)}chars")
        try:
            from agents.wiki.wiki_manager import (
                write_wiki_entry, preflight_wiki_search, merge_wiki_entry
            )

            if session_notes:
                write_wiki_entry(
                    wiki_type="session_notes",
                    name=f"session_{session_id}",
                    content=session_notes,
                    description=f"Session notes for session {session_id}",
                    session_id=session_id,
                )
                print(f"[wiki_write] session_notes written for session={session_id}")

            if project_knowledge:
                similar = await preflight_wiki_search(project_knowledge, "knowledge")

                if similar:
                    top_entry, top_score = similar[0]
                    if top_score >= 0.85:
                        merge_wiki_entry(top_entry, project_knowledge, mode="replace")
                        print(f"[wiki_write] knowledge merged (replace) to {top_entry.rel_path} score={top_score:.2f}")
                        return
                    elif top_score >= 0.70:
                        merge_wiki_entry(top_entry, project_knowledge, mode="append")
                        print(f"[wiki_write] knowledge merged (append) to {top_entry.rel_path} score={top_score:.2f}")
                        return

                write_wiki_entry(
                    wiki_type="knowledge",
                    name=f"from_session_{session_id}",
                    content=project_knowledge,
                    description=f"Project knowledge extracted from session {session_id}",
                )
                print(f"[wiki_write] knowledge written for session={session_id}")
        except Exception as e:
            print(f"[wiki_write] failed session={session_id}: {type(e).__name__}: {e}")

    def _build_transcript(self, events: list[dict]) -> str:
        """构建对话文本用于摘要。"""
        lines = []
        for e in events:
            t = e.get("type", "")
            if t == "user_message":
                role = "user"
            elif t == "assistant_message":
                role = "assistant"
            elif t == "tool_result_msg":
                role = "tool result"
            else:
                continue
            content = e.get("content", "")[:500]
            lines.append(f"[{role}]: {content}")
        return "\n".join(lines)

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
