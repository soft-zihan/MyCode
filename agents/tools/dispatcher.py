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

from agents.core.frontmatter import parse_frontmatter
from agents.tools.registry import EDIT_TOOLS
from agents.tools.result import ToolExecutionResult
from agents.wiki.store import get_wiki_dir
from agents.core.context_events import collect_hidden_seqs
from agents.core.snapshot_service import SnapshotService
from agents.core.subagent_runner import execute_agent_tool, execute_subagent_cancel_tool
from agents.logging import print_info, print_error
from agents.observability.trace import trace_span
from agents.plan.plan_tool_executor import execute_plan_mode_tool
from agents.skills.skills import discover_skills, execute_skill
from agents.tools import execute_tool
from agents.tools.question_tools import handle_ask_user
from agents.tools.task_tools import handle_task_list
from agents.tools.wiki_tools import remember


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

    async def execute_tool_call(self, name: str, inp: dict,
                                assistant_seq: int | None = None) -> ToolExecutionResult:
        """执行工具调用（带超时、结构化 outcome 和 trace）。

        assistant_seq: 承载本次 tool_calls 的 assistant 消息的 seq，由 agent_loop
        在每个模型响应处捕获一次后沿工具执行链传入。只有需要事件 seq 记账的
        工具（task_list）用它；其余工具忽略。
        """
        from contextlib import nullcontext


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
                    self._execute_tool_call_inner(name, inp, assistant_seq),
                    timeout=timeout,
                )
                duration_s = round(time.time() - _tool_t0, 2)
                print_info(f"[DEBUG] execute_tool_call: {name} done, took {duration_s}s")
                return self._success_result(span, result, name, duration_s, timeout)
            except asyncio.TimeoutError:
                error = TimeoutError(f"Tool '{name}' timed out after {timeout}s")
                return self._timeout_result(span, name, round(time.time() - _tool_t0, 2), timeout,
                                            detail=f"timed out after {timeout}s", error=error)
            except TimeoutError as e:
                return self._timeout_result(span, name, round(time.time() - _tool_t0, 2), timeout,
                                            detail=f"timed out: {e}", error=e)
            except Exception as e:
                # BC-4：工具运行时异常反馈给模型自行恢复（与 timeout 路径同语义），
                # 不再杀死整个 turn（曾导致 grep 超时 → turn/end reason=error）
                duration_s = round(time.time() - _tool_t0, 2)
                if span:
                    span.record_error(e)
                    span.add_metadata(duration_s=duration_s, outcome="error", success=False)
                print_error(f"[ERROR] Tool '{name}' failed: {type(e).__name__}: {e}")
                return ToolExecutionResult(
                    text=f"Error: tool '{name}' failed: {type(e).__name__}: {e}",
                    status="error",
                    outcome="error",
                    metadata={"tool_name": name, "duration_s": duration_s, "timeout_s": timeout},
                )

    def _success_result(self, span, result: str | ToolExecutionResult, name: str,
                        duration_s: float, timeout: int) -> ToolExecutionResult:
        """成功路径：str 结果封装为 ToolExecutionResult；结构化结果补默认 metadata + span 上报。"""
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

    def _timeout_result(self, span, name: str, duration_s: float, timeout: int,
                        *, detail: str, error: Exception) -> ToolExecutionResult:
        """超时路径（asyncio.TimeoutError / 显式 TimeoutError 统一封装）。"""
        if span:
            span.record_error(error)
            span.add_metadata(timeout_s=timeout, duration_s=duration_s, outcome="timeout", success=False)
        print_error(f"[ERROR] Tool '{name}' {detail}")
        return ToolExecutionResult(
            text=f"Error: tool '{name}' {detail}",
            status="error",
            outcome="timeout",
            metadata={"tool_name": name, "duration_s": duration_s, "timeout_s": timeout},
        )

    async def _execute_tool_call_inner(self, name: str, inp: dict,
                                       assistant_seq: int | None = None) -> str | ToolExecutionResult:
        """工具执行内部路由。"""
        if name == "compact_context":
            return await self._execute_compact_context_tool(inp)
        if name == "context_restore":
            return self._execute_context_restore_tool(inp)
        if name == "search_history":
            return self._execute_search_history_tool(inp)
        if name == "remember":
            return await remember(inp, side_query=self.agent._build_side_query(max_tokens=6000))
        if name == "list_session_notes":
            return self._execute_list_session_notes_tool(inp)
        if name == "git_diff_before_last_compress":
            return await self._execute_git_diff_before_last_compress_tool(inp)
        if name == "git_diff_session":
            return await self._execute_git_diff_session_tool(inp)
        if name in ("enter_plan_mode", "exit_plan_mode"):
            return await execute_plan_mode_tool(self.agent, name)
        if name == "agent":
            return await execute_agent_tool(self.agent, inp, timeout_s=self.get_tool_timeout("agent"))
        if name == "subagent_cancel":
            return await execute_subagent_cancel_tool(self.agent, inp)
        if name == "ask_user":
            return await handle_ask_user(self.agent.session, inp, abort_fn=lambda: self.agent.abort_requested())
        if name == "task_list":
            if self.agent.permission_mode == "plan":
                return (
                    "Error: task_list is disabled in plan mode. Write the plan into "
                    "tasks.md; approved tasks are materialized into task_list automatically."
                )
            # current_seq = 承载本次 tool_calls 的 assistant 消息的 seq，由
            # agent_loop 在每个模型响应处捕获一次（_run_step 落盘
            # assistant_message 时），再沿 _handle_tool_calls →
            # _execute_tool_batches → 并发批/顺序批 → execute_tool_call 传进来。
            # 因为是「每次响应捕获一次的传值」，同一批次里先执行的工具动不了它。
            #
            # 刻意不读 self.agent.last_usage_seq：那个属性虽然也在这里被写成
            # 同一个值，但它同时是 token 估算的可变状态，而 compact_context
            # 是被广告出去的工具，折叠成功就会经 _compact_conversation →
            # reset_context_token_estimate（agents/core/context.py:100）把它置回
            # -1；compact_context 与 task_list 同为 sequential，模型一批
            # [compact_context, task_list] 就会让 task_list 读到 -1。
            #
            # 也不能用 session.seq - 1：assistant_message 之后 step/end 还会占
            # 一个 seq，同批次先行工具的 tool_result_msg 也先落盘。
            #
            # 负值夹成 None（防御层）：None 走已分析过的安全路径——started_seq
            # 保持未写、后续状态流转仍能补上，detail_origin_seq=None 只多披露
            # 一次；而 -1 会被 store 的 write-once 守卫永久钉住，Plan 2 的验收
            # 闸门要靠 started_seq 界定事件扫描区间。
            current_seq = (
                assistant_seq
                if assistant_seq is not None and assistant_seq >= 0
                else None
            )
            result = handle_task_list(self.agent.session.id, inp, current_seq=current_seq)
            self.agent.session.append("task_list/updated", {"session_id": self.agent.session.id})
            return result
        if name == "skill":
            return await self._execute_skill_tool(inp)
        if self.agent._mcp_manager.is_mcp_tool(name):
            return await self.agent._mcp_manager.call_tool(name, inp)

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
                    self.agent.refresh_runtime_system_prompt(force=True)
            except Exception:
                pass
        return result

    async def _execute_skill_tool(self, inp: dict) -> str | ToolExecutionResult:
        """skill 工具：解析注册 skill 并返回其指令（inline），fork 上下文转子代理执行。

        历史（BC-28）：该工具曾在早期重构中被误删，但 system prompt 的
        Available Skills 段一直广告"call the `skill` tool"——模型调用只会得到
        Unknown tool。U9（模型主动调 skill 生成图表）依赖此通路，按原契约恢复。
        """

        skill_name = str(inp.get("skill_name") or "").strip()
        if not skill_name:
            return "Error: skill_name is required."
        result = execute_skill(skill_name, inp.get("args") or "", self._skill_substitutions())
        if result is None:
            available = ", ".join(sorted(s.name for s in discover_skills()))
            return f"Error: unknown skill '{skill_name}'. Available skills: {available}"
        if result.get("context") == "fork":
            # fork：独立子代理执行 skill 指令，保护主上下文窗口
            return await execute_agent_tool(
                self.agent,
                {"type": "general", "prompt": result["prompt"], "description": f"skill:{skill_name}"},
                timeout_s=self.get_tool_timeout("agent"),
            )
        return result["prompt"]

    def _skill_substitutions(self) -> dict[str, str]:
        """U9：skill 模板运行时变量——会话 ID 与 artifacts 图片输出目录。

        SKILL.md 正文可用 ${SESSION_ID} / ${ARTIFACTS_DIR}；artifacts 目录
        约定 <workspace>/.mycode/artifacts/<session_id>/，由 GET
        /api/artifacts/{session_id}/{filename} 只读伺服（前端内联 <img>）。
        """
        from pathlib import Path

        session_id = self.agent.session_id
        artifacts_dir = ""
        cwd = self.agent.session.projections.get("cwd")
        if cwd:
            artifacts_dir = str(Path(cwd) / ".mycode" / "artifacts" / session_id)
        return {"${SESSION_ID}": session_id, "${ARTIFACTS_DIR}": artifacts_dir}

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
