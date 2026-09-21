"""Tool Dispatcher — 工具调度逻辑封装。

职责：
- 工具调度 + 执行
- 工具超时管理
- 各工具实现（compact_context、context_restore、search_history 等）
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any

from agents.tools.registry import EDIT_TOOLS
from agents.tools.result import ToolExecutionResult


class ToolDispatcher:
    """工具调度管理器。"""

    def __init__(self, *, agent_ref: Any):
        """
        Args:
            agent_ref: Agent 实例引用，用于访问 session、workspace 等
        """
        self.agent = agent_ref

    def get_tool_timeout(self, name: str) -> int:
        """获取工具超时时间（秒）。"""
        if name == "run_shell":
            return 300
        if name == "agent":
            return int(os.environ.get("MYCODE_AGENT_TOOL_TIMEOUT", "900"))
        if name == "skill":
            return int(os.environ.get("MYCODE_SKILL_TOOL_TIMEOUT", "300"))
        if name == "exit_plan_mode":
            return 600
        if name in ("read_file", "outline_file", "grep_search", "list_files", "web_search"):
            return 30
        return 60

    async def execute_tool_call(self, name: str, inp: dict) -> ToolExecutionResult:
        """执行工具调用（带超时、结构化 outcome 和 trace）。"""
        from contextlib import nullcontext

        from agents.observability.trace import trace_span
        from agents.logging import print_info, print_error

        _tool_t0 = time.time()
        try:
            _inp_preview = json.dumps(inp, ensure_ascii=False, default=str)[:4000]
        except (TypeError, ValueError):
            _inp_preview = str(inp)[:4000]

        trace_metadata: dict[str, Any] = {"tool_name": name}
        if self.agent.current_sub_agent_id:
            trace_metadata["sub_agent_id"] = self.agent.current_sub_agent_id

        timeout = self.get_tool_timeout(name)
        print_info(f"[DEBUG] execute_tool_call: {name}, timeout={timeout}s")

        span_cm = (
            nullcontext(None)
            if name == "agent"
            else trace_span("tool_call", name=f"tool.{name}", input=_inp_preview, metadata=trace_metadata)
        )
        with span_cm as span:
            try:
                print_info(f"[DEBUG] execute_tool_call: calling asyncio.wait_for for {name}")
                result = await asyncio.wait_for(
                    self._execute_tool_call_inner(name, inp),
                    timeout=timeout,
                )
                duration_s = round(time.time() - _tool_t0, 2)
                print_info(f"[DEBUG] execute_tool_call: {name} done, took {duration_s}s")
                if isinstance(result, ToolExecutionResult):
                    success = result.status == "ok" and result.outcome == "success"
                    result.metadata.setdefault("tool_name", name)
                    result.metadata.setdefault("duration_s", duration_s)
                    result.metadata.setdefault("timeout_s", timeout)
                    if span:
                        span.update(
                            output=result.text[:4000],
                            metadata={
                                "success": success,
                                "outcome": result.outcome,
                                "duration_s": duration_s,
                                "result_chars": len(result.text),
                                **{k: v for k, v in result.metadata.items() if k not in {"tool_name", "duration_s", "timeout_s"}},
                            },
                        )
                    return result

                if span:
                    span.update(
                        output=str(result)[:4000],
                        metadata={
                            "success": True,
                            "outcome": "success",
                            "duration_s": duration_s,
                            "result_chars": len(result),
                        },
                    )
                return ToolExecutionResult(
                    text=result,
                    status="ok",
                    outcome="success",
                    metadata={"tool_name": name, "duration_s": duration_s, "timeout_s": timeout},
                )
            except asyncio.TimeoutError:
                duration_s = round(time.time() - _tool_t0, 2)
                error = TimeoutError(f"Tool '{name}' timed out after {timeout}s")
                if span:
                    span.record_error(error)
                    span.add_metadata(timeout_s=timeout, duration_s=duration_s, outcome="timeout", success=False)
                print_error(f"[ERROR] Tool '{name}' timed out after {timeout}s")
                return ToolExecutionResult(
                    text=f"Error: tool '{name}' timed out after {timeout}s",
                    status="error",
                    outcome="timeout",
                    metadata={"tool_name": name, "duration_s": duration_s, "timeout_s": timeout},
                )
            except TimeoutError as e:
                duration_s = round(time.time() - _tool_t0, 2)
                if span:
                    span.record_error(e)
                    span.add_metadata(timeout_s=timeout, duration_s=duration_s, outcome="timeout", success=False)
                print_error(f"[ERROR] Tool '{name}' timed out: {e}")
                return ToolExecutionResult(
                    text=f"Error: tool '{name}' timed out: {e}",
                    status="error",
                    outcome="timeout",
                    metadata={"tool_name": name, "duration_s": duration_s, "timeout_s": timeout},
                )
            except Exception as e:
                if span:
                    span.record_error(e)
                    span.add_metadata(duration_s=round(time.time() - _tool_t0, 2), outcome="error", success=False)
                print_error(f"[ERROR] Tool '{name}' failed: {type(e).__name__}: {e}")
                raise

    async def _execute_tool_call_inner(self, name: str, inp: dict) -> str | ToolExecutionResult:
        """工具执行内部路由。"""
        if name == "compact_context":
            return await self._execute_compact_context_tool(inp)
        if name == "context_restore":
            return self._execute_context_restore_tool(inp)
        if name == "search_history":
            return self._execute_search_history_tool(inp)
        if name == "list_session_notes":
            return self._execute_list_session_notes_tool(inp)
        if name == "git_diff_before_last_compress":
            return await self._execute_git_diff_before_last_compress_tool(inp)
        if name == "git_diff_session":
            return await self._execute_git_diff_session_tool(inp)
        if name in ("enter_plan_mode", "exit_plan_mode"):
            return await self.agent._execute_plan_mode_tool(name)
        if name == "agent":
            return await self._execute_agent_tool(inp)
        if name == "ask_user":
            from agents.tools.question_tools import handle_ask_user
            return await handle_ask_user(self.agent.session, inp, abort_fn=lambda: self.agent.abort_requested())
        if name == "todolist":
            if self.agent.permission_mode == "plan":
                return "Error: todolist is disabled in plan mode. Use the plan system's tasks.md instead."
            from agents.tools.todo_tools import handle_todolist
            result = handle_todolist(self.agent.session.id, inp)
            self.agent.session.append("todo/updated", {"session_id": self.agent.session.id})
            return result
        if self.agent._mcp_manager.is_mcp_tool(name):
            return await self.agent._mcp_manager.call_tool(name, inp)

        from agents.logging import print_info
        from agents.tools import execute_tool
        print_info(f"[DEBUG] execute_tool_call_inner: calling execute_tool for {name}")
        result = await execute_tool(name, inp, self.agent._read_file_state)
        print_info(f"[DEBUG] execute_tool_call_inner: execute_tool done for {name}")

        if (
            name.startswith("plan_")
            and name in EDIT_TOOLS
            and isinstance(result, str)
            and not result.startswith("Error")
        ):
            self.agent.session.append("plan/updated", {
                "session_id": self.agent.session.id,
                "tool": name,
                "slug": inp.get("slug", ""),
            })

        if (
            name in ("write_file", "edit_file")
            and isinstance(result, str)
            and not result.startswith("Error")
            and self.agent.permission_mode == "plan"
            and self.agent._plan_mode_manager.plan_dir
        ):
            _draft_dir = str(self.agent._plan_mode_manager.plan_dir)
            if str(inp.get("file_path", "")).startswith(_draft_dir):
                self.agent.session.append("plan/updated", {
                    "session_id": self.agent.session.id,
                    "tool": name,
                    "slug": "",
                })

        if name == "skill_create":
            try:
                parsed = json.loads(result)
                if isinstance(parsed, dict) and parsed.get("ok"):
                    self.agent.refresh_runtime_system_prompt()
            except Exception:
                pass
        return result

    async def _execute_compact_context_tool(self, inp: dict) -> str:
        """执行 compact_context 工具。"""
        reason = str(inp.get("reason") or "").strip()
        compacted = await self.agent._compact_conversation(trigger="tool")
        if not compacted:
            self.agent._record_tool_outcome("compact_context", False)
            return "No context compaction was performed because there is not enough conversation history yet."
        self.agent._record_tool_outcome("compact_context", True)
        self.agent._context_cleared = True
        suffix = f"\nReason: {reason}" if reason else ""
        return (
            "Context compacted into structured session memory. "
            "Continue from the folded memory now present in the conversation context."
            f"{suffix}"
        )

    def _execute_context_restore_tool(self, inp: dict) -> str:
        """执行 context_restore 工具。"""
        key = str(inp.get("key") or "").strip()
        if not key:
            return "Error: 'key' is required. Use the call_id from the tool_folded placeholder."

        call_id = key.removeprefix("snip:")

        from agents.core.context_events import collect_hidden_seqs

        hidden_seqs = collect_hidden_seqs(self.agent.session.events)

        for event in self.agent.session._log:
            if event.get("type") == "tool_result_msg" and event.get("call_id") == call_id:
                if event.get("seq") in hidden_seqs:
                    return event.get("content", "")

        available = []
        for event in self.agent.session._log:
            if event.get("type") == "tool_result_msg" and event.get("seq") in hidden_seqs:
                available.append(f"snip:{event.get('call_id')}")

        hint = f" Available keys: {', '.join(available[:20])}" if available else " No restorable content found."
        return f"Error: no restorable content for key '{key}'.{hint}"

    def _execute_search_history_tool(self, inp: dict) -> str:
        """执行 search_history 工具。"""
        query = str(inp.get("query") or "").strip().lower()
        if not query:
            return "Error: 'query' is required."

        limit = int(inp.get("limit") or 20)

        from agents.core.context_events import collect_hidden_seqs

        hidden_seqs = collect_hidden_seqs(self.agent.session.events)

        results = []
        for event in self.agent.session._log:
            t = event.get("type")
            if t not in ("user_message", "assistant_message", "tool_result_msg"):
                continue

            content = event.get("content", "")
            if isinstance(content, list):
                content = " ".join(str(c) for c in content)
            content_str = str(content).lower()

            if query in content_str:
                seq = event.get("seq", 0)
                call_id = event.get("call_id", "")
                is_hidden = seq in hidden_seqs

                preview = str(content)[:200] if isinstance(content, str) else str(content[0])[:200]

                results.append({
                    "seq": seq,
                    "type": t,
                    "content_preview": preview,
                    "call_id": call_id,
                    "hidden": is_hidden,
                })

        if not results:
            return f"No results found for '{query}'."

        result_lines = [f"Found {len(results)} results for '{query}':"]
        for r in results[:limit]:
            hidden_marker = " [HIDDEN]" if r["hidden"] else ""
            result_lines.append(f"  [seq={r['seq']}] {r['type']}{hidden_marker}: {r['content_preview']}")

        restorable = [r for r in results if r.get("call_id") and r["hidden"]]
        if restorable:
            result_lines.append("\n--- Restorable tool results ---")
            for r in restorable[:10]:
                result_lines.append(f"  call_id={r['call_id']}: {r['content_preview'][:100]}...")
                result_lines.append(f"  → Use context_restore(key='snip:{r['call_id']}') to restore")

        return "\n".join(result_lines)

    def _execute_list_session_notes_tool(self, inp: dict) -> str:
        limit = int(inp.get("limit") or 10)

        from agents.core.frontmatter import parse_frontmatter
        from agents.wiki.wiki_manager import get_wiki_dir

        wiki_dir = get_wiki_dir() / "session_notes"
        if not wiki_dir.exists():
            return "No session notes found."

        notes = []
        for filepath in wiki_dir.glob("*.md"):
            try:
                parsed = parse_frontmatter(filepath.read_text())
                meta = parsed.meta
                body = parsed.body
                session_id = str(meta.get("session_id") or filepath.stem.removeprefix("session_"))
                notes.append({
                    "session_id": session_id,
                    "title": meta.get("name", session_id),
                    "time": meta.get("modified", ""),
                    "content": body,
                })
            except Exception as exc:
                print(f"[list_session_notes] failed to read {filepath.name}: {type(exc).__name__}: {exc}")

        if not notes:
            return "No session notes found."

        notes.sort(key=lambda x: x["time"], reverse=True)
        notes = notes[:limit]

        result_lines = [f"Found {len(notes)} session notes:"]
        for note in notes:
            result_lines.append(f"  [{note['session_id']}] {note['title']} ({note['time']})")

        latest = notes[0]
        result_lines.append(f"\n--- Latest note ({latest['session_id']}) ---")
        result_lines.append(latest["content"])

        return "\n".join(result_lines)

    async def _execute_git_diff_before_last_compress_tool(self, inp: dict) -> str:
        """执行 git_diff_before_last_compress 工具。"""
        from agents.core.snapshot_service import SnapshotService
        from pathlib import Path

        session_id = self.agent.session_id
        snapshots_dir = Path.home() / ".mycode" / "snapshots"
        svc = SnapshotService(self.agent.workspace, str(snapshots_dir))

        # 获取当前 session 的所有 snapshot
        snapshots = await svc.list(session_id=session_id)
        if len(snapshots) < 2:
            return "No compression history found for current session."

        # 找到最后一次压缩的 snapshot（label 包含 "compress" 的）
        compress_snapshots = [s for s in snapshots if s.label and "compress" in s.label]
        if len(compress_snapshots) < 2:
            return "Not enough compression history found."

        # 上上次压缩 → 上次压缩
        to_snapshot = compress_snapshots[0]  # 最新的压缩
        from_snapshot = compress_snapshots[1]  # 上一次的压缩

        # 获取 diff
        file_diffs = await svc.diff(from_snapshot.id, to_snapshot.id)

        # 过滤文件
        files_filter = inp.get("files")
        if files_filter:
            file_diffs = [d for d in file_diffs if d.path in files_filter]

        if not file_diffs:
            return "No file changes found."

        # 返回文件列表
        lines = [f"Files changed between compress {from_snapshot.id[:8]} → {to_snapshot.id[:8]}:"]
        for d in file_diffs:
            lines.append(f"  {d.status:8s} {d.path}")

        return "\n".join(lines)

    async def _execute_git_diff_session_tool(self, inp: dict) -> str:
        """执行 git_diff_session 工具。"""
        from agents.core.snapshot_service import SnapshotService
        from pathlib import Path

        session_id = str(inp.get("session_id") or "").strip()
        if not session_id:
            return "Error: session_id is required."

        snapshots_dir = Path.home() / ".mycode" / "snapshots"
        svc = SnapshotService(self.agent.workspace, str(snapshots_dir))

        # 获取指定 session 的所有 snapshot
        snapshots = await svc.list(session_id=session_id)
        if len(snapshots) < 2:
            return f"Not enough snapshots found for session '{session_id}'."

        # 第一个 → 最后一个
        from_snapshot = snapshots[-1]  # 最早的
        to_snapshot = snapshots[0]  # 最新的

        # 获取 diff
        file_diffs = await svc.diff(from_snapshot.id, to_snapshot.id)

        # 过滤文件
        files_filter = inp.get("files")
        if files_filter:
            file_diffs = [d for d in file_diffs if d.path in files_filter]

        if not file_diffs:
            return "No file changes found."

        # 返回文件列表
        lines = [f"Files changed in session {session_id}:"]
        for d in file_diffs:
            lines.append(f"  {d.status:8s} {d.path}")

        return "\n".join(lines)

    async def _execute_agent_tool(self, inp: dict) -> str | ToolExecutionResult:
        """执行 agent 工具（子 Agent）。"""
        from agents.observability.trace import trace_span
        from agents.logging import print_sub_agent_start, print_sub_agent_end
        from agents.core.subagent import get_sub_agent_config
        from agents.core.session import Session
        import uuid

        agent_type = inp.get("type", "general")
        description = inp.get("description", "sub-agent task")
        prompt = inp.get("prompt", "")
        timeout_s = self.get_tool_timeout("agent")
        config = get_sub_agent_config(agent_type)
        max_tool_calls = config.get("max_tool_calls")
        print_sub_agent_start(agent_type, description)

        sub_agent_id = str(uuid.uuid4())[:8]

        with trace_span(
            "sub_agent",
            name=f"agent.{agent_type}",
            input=prompt[:4000],
            metadata={
                "agent_id": sub_agent_id,
                "agent_type": agent_type,
                "description": description[:500],
                "parent_session_id": self.agent.session_id,
                "timeout_s": timeout_s,
                "max_tool_calls": max_tool_calls,
            },
        ) as span:
            sub_session = Session(
                session_id=sub_agent_id,
                parent_session=self.agent.session_id,
                origin="sub_agent",
                agent_type=agent_type,
            )

            self.agent.session.append("sub_agent/start", {
                "agent_id": sub_agent_id,
                "agent_type": agent_type,
                "description": description,
                "sub_session_id": sub_session.id,
                "timeout_s": timeout_s,
                "max_tool_calls": max_tool_calls,
            })

            sub_agent = self.agent._spawn_sub_agent(
                system_prompt=config["system_prompt"],
                tools=config["tools"],
                model_ref=config.get("model_ref", ""),
                label=agent_type,
                max_tool_calls=max_tool_calls,
            )

            sub_agent.session = sub_session
            sub_agent.session_id = sub_session.id
            sub_agent._current_sub_agent_id = sub_agent_id

            start_time = time.time()
            status = "error"
            outcome = "error"
            summary = ""
            output_text = ""
            input_tokens = 0
            output_tokens = 0
            stop_reason: str | None = None
            error: Exception | None = None
            try:
                result = await asyncio.create_task(sub_agent.run_once(prompt))
                input_tokens = int(result.get("tokens", {}).get("input", 0))
                output_tokens = int(result.get("tokens", {}).get("output", 0))
                self.agent.total_input_tokens += input_tokens
                self.agent.total_output_tokens += output_tokens
                output_text = result.get("text") or "(Sub-agent produced no output)"
                summary = output_text[:500]
                stop_reason = result.get("stop_reason")
                if sub_agent._aborted:
                    status = "aborted"
                    outcome = "cancelled"
                    return ToolExecutionResult(
                        text="(Sub-agent aborted)",
                        status="cancelled",
                        outcome="cancelled",
                        metadata={"reason": "aborted", "sub_session_id": sub_session.id},
                    )
                if result.get("tool_budget_exceeded"):
                    status = "budget_exceeded"
                    outcome = "budget_exceeded"
                    summary = (
                        f"Sub-agent stopped after tool budget: "
                        f"{result.get('tool_call_count', 0)}/{max_tool_calls}. {summary}"
                    ).strip()
                    return ToolExecutionResult(
                        text=output_text,
                        status="ok",
                        outcome="budget_exceeded",
                        metadata={
                            "reason": "tool_budget_exceeded",
                            "tool_call_count": int(getattr(sub_agent, "_tool_call_count", 0) or 0),
                            "failed_tool_call_count": int(getattr(sub_agent, "_failed_tool_call_count", 0) or 0),
                            "max_tool_calls": max_tool_calls,
                            "partial": True,
                            "sub_session_id": sub_session.id,
                        },
                    )
                if stop_reason:
                    status = "completed"
                    outcome = "blocked"
                    summary = f"Sub-agent stopped by {stop_reason}. {summary}".strip()
                    return ToolExecutionResult(
                        text=output_text,
                        status="ok",
                        outcome="blocked",
                        metadata={
                            "reason": stop_reason,
                            "tool_call_count": int(getattr(sub_agent, "_tool_call_count", 0) or 0),
                            "failed_tool_call_count": int(getattr(sub_agent, "_failed_tool_call_count", 0) or 0),
                            "max_tool_calls": max_tool_calls,
                            "partial": True,
                            "sub_session_id": sub_session.id,
                        },
                    )
                status = "completed"
                outcome = "success"
                return ToolExecutionResult(
                    text=output_text,
                    status="ok",
                    outcome="success",
                    metadata={
                        "tool_call_count": int(getattr(sub_agent, "_tool_call_count", 0) or 0),
                        "failed_tool_call_count": int(getattr(sub_agent, "_failed_tool_call_count", 0) or 0),
                        "max_tool_calls": max_tool_calls,
                        "sub_session_id": sub_session.id,
                    },
                )
            except asyncio.CancelledError:
                if self.agent.abort_requested():
                    status = "cancelled"
                    outcome = "cancelled"
                    summary = "Sub-agent cancelled by parent abort"
                else:
                    status = "timeout"
                    outcome = "timeout"
                    summary = f"Sub-agent timed out after {timeout_s}s"
                    error = TimeoutError(summary)
                output_text = summary
                raise
            except Exception as e:
                status = "error"
                outcome = "error"
                summary = f"{type(e).__name__}: {e}"
                output_text = f"Sub-agent error: {e}"
                error = e
                return ToolExecutionResult(
                    text=output_text,
                    status="error",
                    outcome="error",
                    metadata={"reason": summary, "sub_session_id": sub_session.id},
                )
            finally:
                duration_s = round(time.time() - start_time, 2)
                tool_call_count = int(getattr(sub_agent, "_tool_call_count", 0) or 0)
                failed_tool_call_count = int(getattr(sub_agent, "_failed_tool_call_count", 0) or 0)
                child_turn_count = max(int(getattr(sub_agent, "_turn_number", 0) or 0), 1 if tool_call_count else 0)
                print_sub_agent_end(agent_type, description)
                self.agent.session.append("sub_agent/end", {
                    "agent_id": sub_agent_id,
                    "status": status,
                    "outcome": outcome,
                    "summary": summary[:500],
                    "duration_ms": int(duration_s * 1000),
                    "sub_session_id": sub_session.id,
                    "timeout_s": timeout_s,
                    "max_tool_calls": max_tool_calls,
                    "stop_reason": stop_reason,
                    "tool_call_count": tool_call_count,
                    "failed_tool_call_count": failed_tool_call_count,
                    "child_turn_count": child_turn_count,
                })
                span_metadata = {
                    "status": status,
                    "outcome": outcome,
                    "duration_s": duration_s,
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "summary": summary[:500],
                    "sub_session_id": sub_session.id,
                    "timeout_s": timeout_s,
                    "max_tool_calls": max_tool_calls,
                    "stop_reason": stop_reason,
                    "tool_call_count": tool_call_count,
                    "failed_tool_call_count": failed_tool_call_count,
                    "child_turn_count": child_turn_count,
                }
                span.update(output=output_text[:20000], metadata=span_metadata)
                if error is not None:
                    span.record_error(error)
