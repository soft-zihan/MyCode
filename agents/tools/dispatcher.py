"""Tool Dispatcher — 工具调度逻辑封装。

职责：
- 工具调度 + 执行
- 工具超时管理
- 各工具实现（compact_context、context_restore、search_history 等）
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any


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
        if name in ("agent", "skill"):
            return 300
        if name == "exit_plan_mode":
            return 600
        if name in ("read_file", "outline_file", "grep_search", "list_files"):
            return 30
        return 60

    async def execute_tool_call(self, name: str, inp: dict) -> str:
        """执行工具调用（带超时和 trace）。"""
        from agents.observability.trace import trace_span
        from agents.logging import print_info, print_error

        _tool_t0 = time.time()
        try:
            _inp_preview = json.dumps(inp, ensure_ascii=False, default=str)[:1000]
        except (TypeError, ValueError):
            _inp_preview = str(inp)[:1000]

        trace_attrs = {
            "langfuse.observation.type": "tool",
            "tool": name,
            "tool.name": name,
            "langfuse.observation.input": _inp_preview,
        }
        if self.agent.current_sub_agent_id:
            trace_attrs["sub_agent_id"] = self.agent.current_sub_agent_id

        timeout = self.get_tool_timeout(name)
        print_info(f"[DEBUG] execute_tool_call: {name}, timeout={timeout}s")

        with trace_span("tool_call", **trace_attrs) as span:
            try:
                print_info(f"[DEBUG] execute_tool_call: calling asyncio.wait_for for {name}")
                result = await asyncio.wait_for(
                    self._execute_tool_call_inner(name, inp),
                    timeout=timeout,
                )
                print_info(f"[DEBUG] execute_tool_call: {name} done, took {time.time()-_tool_t0:.2f}s")
                span.set_attribute("success", True)
                span.set_attribute("duration_s", round(time.time() - _tool_t0, 2))
                span.set_attribute("langfuse.observation.output", str(result)[:2000])
            except asyncio.TimeoutError:
                span.record_error(TimeoutError(f"Tool '{name}' timed out after {timeout}s"))
                print_error(f"[ERROR] Tool '{name}' timed out after {timeout}s")
                return f"Error: tool '{name}' timed out after {timeout}s"
            except TimeoutError as e:
                span.record_error(e)
                print_error(f"[ERROR] Tool '{name}' timed out: {e}")
                return f"Error: tool '{name}' timed out: {e}"
            except Exception as e:
                span.record_error(e)
                print_error(f"[ERROR] Tool '{name}' failed: {type(e).__name__}: {e}")
                raise
        return result

    async def _execute_tool_call_inner(self, name: str, inp: dict) -> str:
        """工具执行内部路由。"""
        if name == "compact_context":
            return await self._execute_compact_context_tool(inp)
        if name == "context_restore":
            return self._execute_context_restore_tool(inp)
        if name == "search_history":
            return self._execute_search_history_tool(inp)
        if name == "list_session_notes":
            return self._execute_list_session_notes_tool(inp)
        if name == "read_session_notes":
            return self._execute_read_session_notes_tool(inp)
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

        hidden_seqs = set()
        for event in self.agent.session._log:
            if event.get("type") == "events_hidden":
                hidden_seqs.update(event.get("hidden_seqs", []))

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

        hidden_seqs = set()
        for event in self.agent.session._log:
            if event.get("type") == "events_hidden":
                hidden_seqs.update(event.get("hidden_seqs", []))

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
        """执行 list_session_notes 工具。"""
        limit = int(inp.get("limit") or 10)

        from agents.wiki.wiki_manager import get_wiki_dir
        wiki_dir = get_wiki_dir() / "session_notes"

        if not wiki_dir.exists():
            return "No session notes found."

        notes = []
        for f in wiki_dir.glob("*.md"):
            try:
                content = f.read_text()
                from agents.memory.frontmatter import parse_frontmatter
                meta, body = parse_frontmatter(content)

                # 文件名格式: session_{session_id}_{timestamp}.md
                parts = f.stem.split("_")
                if len(parts) >= 3:
                    session_id = parts[1]
                    timestamp = "_".join(parts[2:])
                else:
                    session_id = f.stem.replace("session_", "")
                    timestamp = ""

                notes.append({
                    "session_id": session_id,
                    "timestamp": timestamp,
                    "title": meta.get("name", session_id),
                    "time": meta.get("modified", ""),
                    "content": body,
                })
            except Exception:
                pass

        if not notes:
            return "No session notes found."

        notes.sort(key=lambda x: x["time"], reverse=True)
        notes = notes[:limit]

        result_lines = [f"Found {len(notes)} session notes:"]
        for note in notes:
            ts = f" [{note['timestamp']}]" if note['timestamp'] else ""
            result_lines.append(f"  [{note['session_id']}{ts}] {note['title']} ({note['time']})")

        if notes:
            latest = notes[0]
            result_lines.append(f"\n--- Latest note ({latest['session_id']}) ---")
            result_lines.append(latest['content'])

        return "\n".join(result_lines)

    def _execute_read_session_notes_tool(self, inp: dict) -> str:
        """执行 read_session_notes 工具。"""
        session_id = str(inp.get("session_id") or "").strip()

        from agents.wiki.wiki_manager import get_wiki_dir
        wiki_dir = get_wiki_dir() / "session_notes"

        if not wiki_dir.exists():
            return "No session notes found."

        if session_id:
            filepath = wiki_dir / f"session_{session_id}.md"
            if not filepath.exists():
                return f"Error: session note for session '{session_id}' not found."

            content = filepath.read_text()
            from agents.memory.frontmatter import parse_frontmatter
            meta, body = parse_frontmatter(content)
            return body

        filepath = wiki_dir / f"session_{self.agent.session_id}.md"
        if not filepath.exists():
            return f"No session notes found for current session '{self.agent.session_id}'."

        content = filepath.read_text()
        from agents.memory.frontmatter import parse_frontmatter
        meta, body = parse_frontmatter(content)
        return body

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

    async def _execute_agent_tool(self, inp: dict) -> str:
        """执行 agent 工具（子 Agent）。"""
        from agents.observability.tracer import tracer
        from agents.logging import print_sub_agent_start, print_sub_agent_end
        from agents.core.subagent import get_sub_agent_config
        from agents.core.session import Session
        from agents.observability.trace import trace_event
        import uuid

        agent_type = inp.get("type", "general")
        description = inp.get("description", "sub-agent task")
        prompt = inp.get("prompt", "")
        print_sub_agent_start(agent_type, description)

        sub_agent_id = str(uuid.uuid4())[:8]

        with tracer.span("sub_agent.execute", {
            "langfuse.observation.type": "agent",
            "mycode.agent.type": agent_type,
            "mycode.agent.id": sub_agent_id,
            "mycode.agent.description": description[:200],
            "mycode.agent.prompt": prompt[:500],
        }) as span:
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
            })
            trace_event("stream.sub_agent_start", agent_id=sub_agent_id, agent_type=agent_type, description=description)

            config = get_sub_agent_config(agent_type)

            sub_agent = self.agent._spawn_sub_agent(
                system_prompt=config["system_prompt"],
                tools=config["tools"],
                model_ref=config.get("model_ref", ""),
                label=agent_type,
            )

            sub_agent.session = sub_session
            sub_agent.session_id = sub_session.id
            sub_agent._current_sub_agent_id = sub_agent_id

            from agents.observability.tracer import set_current_session_id
            parent_session_id = self.agent.session_id
            set_current_session_id(parent_session_id)

            start_time = time.time()
            try:
                result = await sub_agent.run_once(prompt)
                duration_s = round(time.time() - start_time, 2)
                self.agent.total_input_tokens += result["tokens"]["input"]
                self.agent.total_output_tokens += result["tokens"]["output"]
                print_sub_agent_end(agent_type, description)
                self.agent.session.append("sub_agent/end", {
                    "agent_id": sub_agent_id,
                    "status": "completed",
                    "summary": (result["text"] or "")[:200],
                    "duration_ms": int(duration_s * 1000),
                    "sub_session_id": sub_session.id,
                })
                trace_event("stream.sub_agent_end", agent_id=sub_agent_id, agent_type=agent_type, status="completed")
                if span:
                    span.set_attribute("mycode.agent.status", "completed")
                    span.set_attribute("mycode.agent.duration_s", duration_s)
                    span.set_attribute("mycode.agent.input_tokens", result["tokens"]["input"])
                    span.set_attribute("mycode.agent.output_tokens", result["tokens"]["output"])
                    span.set_attribute("mycode.agent.summary", (result["text"] or "")[:500])
                if sub_agent._aborted:
                    if span:
                        span.set_attribute("mycode.agent.status", "aborted")
                    return "(Sub-agent aborted)"
                return result["text"] or "(Sub-agent produced no output)"
            except Exception as e:
                duration_s = round(time.time() - start_time, 2)
                print_sub_agent_end(agent_type, description)
                self.agent.session.append("sub_agent/end", {
                    "agent_id": sub_agent_id,
                    "status": "error",
                    "summary": str(e),
                    "duration_ms": int(duration_s * 1000),
                    "sub_session_id": sub_session.id,
                })
                trace_event("stream.sub_agent_end", agent_id=sub_agent_id, agent_type=agent_type, status="error", error=str(e))
                if span:
                    span.set_attribute("mycode.agent.status", "error")
                    span.set_attribute("mycode.agent.duration_s", duration_s)
                    span.record_error(e)
                return f"Sub-agent error: {e}"
