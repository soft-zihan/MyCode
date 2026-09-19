"""Agent 推理循环。

负责与 LLM 的交互循环：发送消息、接收响应、处理工具调用。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agents.tools.registry import CONCURRENCY_SAFE_TOOLS, get_active_tool_definitions
from agents.observability.tool_tracker import ToolCallTracker, check_tool_warnings

if TYPE_CHECKING:
    from agents.agent import Agent


def _safe_utf8_text(text: str) -> str:
    if not text:
        return text
    try:
        text.encode("utf-8")
        return text
    except UnicodeEncodeError:
        return text.encode("utf-8", errors="replace").decode("utf-8")


def _sanitize_for_utf8(obj: Any) -> Any:
    if isinstance(obj, str):
        return _safe_utf8_text(obj)
    if isinstance(obj, list):
        return [_sanitize_for_utf8(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _sanitize_for_utf8(v) for k, v in obj.items()}
    return obj


def _to_openai_tools(tools: list[dict]) -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t["description"],
                "parameters": t["input_schema"],
            },
        }
        for t in tools
    ]


class AgentLoop:
    """Agent 推理循环。

    职责：
    - run(): 主循环入口
    - call_model_stream(): 流式模型调用
    - 重试逻辑
    - 流式事件分发
    """

    def __init__(self, agent: Agent):
        self._agent = agent
        self._tool_tracker = ToolCallTracker()

    def _auto_mark_bad_case(self, signal_type: str, diagnosis: dict) -> None:
        """自动标记 bad case（走歪路或 trace 信号异常）。"""
        try:
            from agents.observability.bad_cases import BadCaseDB, BadCaseSource, Severity
            db = BadCaseDB()
            session_id = self._agent.session.session_id or "unknown"
            db.create(
                session_id=session_id,
                source=BadCaseSource.AUTO_DETECT,
                status="pending",
                severity=Severity.MEDIUM,
                turn_number=self._agent._current_turn,
                step_number=self._agent._current_step,
                signal_type=signal_type,
                diagnosis=diagnosis,
            )
        except Exception:
            pass  # 静默失败，不影响主流程

    def _check_trace_signals(self) -> None:
        """Turn 结束时检测 trace 信号异常，自动创建 bad case。"""
        try:
            from agents.observability.trace_signals import detect_span_errors, detect_high_tool_failure_rate
            trace_id = getattr(self._agent.session, "langfuse_trace_id", None)
            if not trace_id:
                return
            
            # 检测 span 错误
            errors = detect_span_errors(trace_id)
            if errors:
                self._auto_mark_bad_case(
                    signal_type="span_error",
                    diagnosis={"errors": errors[:3], "count": len(errors)}
                )
            
            # 检测工具失败率过高
            failure_info = detect_high_tool_failure_rate(trace_id)
            if failure_info.get("is_high"):
                self._auto_mark_bad_case(
                    signal_type="高工具失败率",
                    diagnosis=failure_info
                )
        except Exception:
            pass  # 静默失败，不影响主流程

    async def run(self, user_message: str) -> None:
        """主推理循环入口。"""
        await self._prepare_turn(user_message)
        
        while True:
            if self._agent.abort_requested():
                self._agent.mark_aborted()
                break

            await self._consume_memory_prefetch()
            await self._consume_wiki_prefetch()

            self._agent.session.append("step/start", {
                "turn": self._agent._current_turn,
                "step": self._agent._current_step + 1,
            })
            self._agent._current_step += 1

            # 步骤开始前拍快照
            start_snapshot = self._capture_file_states()
            self._agent.session.append("snapshot/start", {
                "turn": self._agent._current_turn,
                "step": self._agent._current_step,
                "phase": "start",
                "files": start_snapshot,
            })

            response = await self.call_model_stream()

            self._update_token_stats(response)

            choice = response.get("choices", [{}])[0] if response.get("choices") else {}
            message = choice.get("message", {})
            
            thinking_content = message.get("thinking")
            content = message.get("content") or ""
            tool_calls = message.get("tool_calls")
            
            # 总是写入 assistant_message（包含 turn 和 step 信息）
            # 即使有 tool_calls 也要写入，否则工具调用信息会丢失
            self._agent.session.append("assistant_message", {
                "turn": self._agent._current_turn,
                "step": self._agent._current_step,
                "thinking": thinking_content,
                "content": content,
                "tool_calls": tool_calls,
            })
            
            # 记录 assistant_message 到 Langfuse trace（不含 thinking）
            from agents.observability.trace import trace_span
            assistant_attrs = {
                "langfuse.observation.type": "generation",
                "langfuse.observation.output": content[:2000] if content else "",
            }
            if tool_calls:
                tool_names = [tc.get("function", {}).get("name", "") for tc in tool_calls if tc.get("type") == "function"]
                assistant_attrs["tool_calls"] = ",".join(tool_names)
                assistant_attrs["langfuse.observation.metadata.tool_calls"] = ",".join(tool_names)
            with trace_span("assistant_message", **assistant_attrs):
                pass
            
            self._agent.session.append("step/end", {
                "turn": self._agent._current_turn,
                "step": self._agent._current_step,
            })

            if not tool_calls:
                # 步骤完成后拍快照
                end_snapshot = self._capture_file_states()
                self._agent.session.append("snapshot/end", {
                    "turn": self._agent._current_turn,
                    "step": self._agent._current_step,
                    "phase": "end",
                    "files": end_snapshot,
                })
                await self._finalize_text_response()
                # Turn 结束，检测 trace 信号异常
                self._check_trace_signals()
                break

            self._agent.increment_turns()
            budget = self._agent.check_budget()
            if budget["exceeded"]:
                from agents.logging import print_info
                print_info(f"Budget exceeded: {budget['reason']}")
                # 异常时也拍快照
                error_snapshot = self._capture_file_states()
                self._agent.session.append("snapshot/end", {
                    "turn": self._agent._current_turn,
                    "step": self._agent._current_step,
                    "phase": "error",
                    "files": error_snapshot,
                    "error": "budget_exceeded",
                })
                # Turn 结束，检测 trace 信号异常
                self._check_trace_signals()
                break

            await self._handle_tool_calls(tool_calls)
            
            # 工具执行后拍快照
            tool_snapshot = self._capture_file_states()
            self._agent.session.append("snapshot/end", {
                "turn": self._agent._current_turn,
                "step": self._agent._current_step,
                "phase": "after_tools",
                "files": tool_snapshot,
            })
            
            self._agent.clear_context_flag()
            self._agent.refresh_runtime_system_prompt()
            await self._agent.check_and_compact()

    def _capture_file_states(self) -> list[dict]:
        """捕获当前文件状态（路径 + hash）。

        只扫描会话工作区（agent.workspace）下的文件，返回文件路径和哈希值的列表。
        """
        cwd = self._agent.workspace
        files = []
        # 扫描工作区下的文件（排除隐藏目录和常见忽略目录）
        ignore_dirs = {".git", ".venv", "node_modules", "__pycache__", ".mypy_cache", ".pytest_cache"}
        try:
            for file_path in cwd.rglob("*"):
                try:
                    if not file_path.is_file():
                        continue
                    # 隐藏/忽略目录判断基于相对工作区的路径（工作区本身可能位于隐藏路径下）
                    rel = file_path.relative_to(cwd)
                    if any(part.startswith(".") or part in ignore_dirs for part in rel.parts):
                        continue
                    content = file_path.read_bytes()
                    files.append({
                        "path": str(rel),
                        "hash": hashlib.md5(content).hexdigest()[:8],
                        "size": len(content),
                    })
                except OSError:
                    continue
        except OSError as e:
            from agents.observability.trace import trace_event
            trace_event("snapshot.capture_failed", error=str(e), workspace=str(cwd))
        return files

    async def _prepare_turn(self, user_message: str) -> None:
        """准备轮次：清理消息、重置状态、创建快照。"""
        from agents.memory.memory import MemoryPrefetch, start_memory_prefetch
        from agents.core.snapshot_service import SnapshotService
        from pathlib import Path
        
        a = self._agent
        user_message = _safe_utf8_text(user_message)
        clean_message = re.sub(r"\n*<retrieved_skills>.*?</retrieved_skills>\s*", "", user_message, flags=re.DOTALL).strip()
        
        # 重置工具调用跟踪器
        self._tool_tracker.reset()
        
        snapshot_id = None
        if not a.is_sub_agent:
            cwd = a.session.projections.get("cwd")
            if cwd:
                try:
                    snapshot_dir = Path.home() / ".mycode" / "snapshots"
                    svc = SnapshotService(cwd, snapshot_dir)
                    snapshot = await svc.capture(
                        session_id=a.session.id,
                        label=f"Before: {clean_message[:50]}",
                    )
                    snapshot_id = snapshot.id
                    print(f"[DEBUG] snapshot created: {snapshot_id}")
                except Exception as e:
                    print(f"[ERROR] Failed to create snapshot: {type(e).__name__}: {e}")
                    import traceback
                    traceback.print_exc()
        
        a.append_user_message(clean_message, snapshot_id=snapshot_id)
        a.reset_repeat_chain()

        if not a.is_sub_agent:
            sq = a.build_side_query()
            if sq:
                a.start_memory_prefetch(user_message, sq)
                a.start_wiki_prefetch(user_message, sq)

    async def _consume_memory_prefetch(self) -> None:
        """消费记忆预取结果。"""
        from agents.memory.memory import format_memories_for_injection
        
        a = self._agent
        if a.memory_prefetch and a.memory_prefetch.settled and not a.memory_prefetch.consumed:
            a.memory_prefetch.consumed = True
            try:
                memories = a.memory_prefetch.task.result()
                if memories:
                    injection_text = format_memories_for_injection(memories)
                    injection_text = _safe_utf8_text(injection_text)
                    a.append_memory_injection(injection_text)
                    for m in memories:
                        a.record_memory_surface(m.path, len(m.content.encode()))
            except Exception:
                pass

    async def _consume_wiki_prefetch(self) -> None:
        a = self._agent
        if a._wiki_prefetch is None or a._wiki_prefetch_consumed:
            return
        # 等待 prefetch 完成（最多 20 秒，embedding 冷启动可能较慢）
        if not a._wiki_prefetch.done():
            try:
                await asyncio.wait_for(asyncio.shield(a._wiki_prefetch), timeout=20.0)
            except asyncio.TimeoutError:
                print(f"[wiki_recall] timeout after 20s, skipping injection")
                return
        a._wiki_prefetch_consumed = True
        try:
            entries = a._wiki_prefetch.result()
            if entries:
                from agents.wiki.wiki_manager import format_wiki_for_injection
                injection_text = format_wiki_for_injection(entries)
                injection_text = _safe_utf8_text(injection_text)
                a.append_memory_injection(injection_text)
                for e in entries:
                    a._wiki_surfaced_at[e.rel_path] = a._turn_number
                print(f"[wiki_recall] injected {len(entries)} entries: {[e.rel_path for e in entries]}")
            else:
                print(f"[wiki_recall] no entries found")
        except Exception as e:
            print(f"[wiki_recall] error: {type(e).__name__}: {e}")

    def _update_token_stats(self, response: dict) -> None:
        """更新 token 统计并发布流式事件。"""
        a = self._agent
        a.last_api_call_time = time.time()

        if response.get("usage"):
            a.add_input_tokens(response["usage"]["prompt_tokens"])
            a.add_output_tokens(response["usage"]["completion_tokens"])
            a.set_last_input_tokens(response["usage"]["prompt_tokens"])
            
            if not a.is_sub_agent:
                cached_tokens = response["usage"].get("cached_tokens", 0)
                a.total_cached_tokens += cached_tokens
                a.session.append("stats", {
                    "input_tokens": a.total_input_tokens,
                    "output_tokens": a.total_output_tokens,
                    "cached_tokens": cached_tokens,
                    "total_cached_tokens": a.total_cached_tokens,
                    "context_window": a.context_window,
                    "last_input_token_count": a.last_input_token_count,
                })

    async def _finalize_text_response(self) -> None:
        """完成文本响应：刷新 markdown、打印成本。"""
        pass

    async def _handle_tool_calls(self, tool_calls: list[dict]) -> None:
        """处理工具调用：权限检查、执行、结果收集。"""
        from agents.tools.permissions import check_permission
        from agents.observability.trace import trace_event
        from agents.logging import print_info

        print_info(f"[DEBUG] _handle_tool_calls: start, {len(tool_calls)} tools")
        a = self._agent
        oai_checked: list[dict] = []

        published_calls: list[dict] = []
        
        for tc in tool_calls:
            if a.abort_requested():
                a.mark_aborted()
                for pub_tc in published_calls:
                    cancel_result = "Action cancelled: user abort."
                    a.publish_tool_result_event(pub_tc["id"], pub_tc["fn"], cancel_result, "cancelled")
                    a.append_tool_message(pub_tc["id"], cancel_result, pub_tc["fn"])
                break

            if tc.get("type") != "function":
                continue

            fn_name = tc["function"]["name"]
            try:
                inp = json.loads(tc["function"]["arguments"])
            except Exception:
                inp = {}

            a.publish_tool_call_event(tc["id"], fn_name, inp)
            published_calls.append({"id": tc["id"], "fn": fn_name})

            perm = check_permission(
                fn_name,
                inp,
                a.permission_mode,
                plan_file_path=a.plan_file_path,
                plan_dir=str(a._plan_mode_manager.plan_dir) if a._plan_mode_manager and a._plan_mode_manager.plan_dir else None,
            )

            if perm["action"] == "deny":
                a.record_tool_outcome(fn_name, False)
                deny_result = f"Action denied: {perm.get('message', '')}"
                oai_checked.append({"tc": tc, "fn": fn_name, "inp": inp, "allowed": False, "result": deny_result})
                a.publish_tool_result_event(tc["id"], fn_name, deny_result, "denied")
                continue

            if perm["action"] == "confirm" and perm.get("message") and perm["message"] not in a.confirmed_paths:
                a.set_current_tool_name(fn_name)
                confirmed = await a.confirm_dangerous(perm["message"])
                a.set_current_tool_name(None)
                if not confirmed:
                    a.record_tool_outcome(fn_name, False)
                    user_deny_result = "User denied this action."
                    oai_checked.append({"tc": tc, "fn": fn_name, "inp": inp, "allowed": False, "result": user_deny_result})
                    a.publish_tool_result_event(tc["id"], fn_name, user_deny_result, "denied")
                    continue
                a.add_confirmed_path(perm["message"])

            oai_checked.append({"tc": tc, "fn": fn_name, "inp": inp, "allowed": True})

        from agents.logging import print_info
        print_info(f"[DEBUG] _handle_tool_calls: calling _execute_tool_batches with {len(oai_checked)} tools")
        await self._execute_tool_batches(oai_checked)
        print_info(f"[DEBUG] _handle_tool_calls: done")

    async def _execute_tool_batches(self, oai_checked: list[dict]) -> None:
        """执行工具批次：并发安全工具并行执行，其他顺序执行。"""
        from agents.observability.trace import trace_event
        from agents.logging import print_info

        print_info(f"[DEBUG] _execute_tool_batches: start, {len(oai_checked)} tools")
        a = self._agent
        oai_batches: list[dict] = []
        
        for ct in oai_checked:
            safe = ct["allowed"] and ct["fn"] in CONCURRENCY_SAFE_TOOLS
            if safe and oai_batches and oai_batches[-1]["concurrent"]:
                oai_batches[-1]["items"].append(ct)
            else:
                oai_batches.append({"concurrent": safe, "items": [ct]})

        print_info(f"[DEBUG] _execute_tool_batches: {len(oai_batches)} batches")
        oai_context_break = False
        for i, batch in enumerate(oai_batches):
            print_info(f"[DEBUG] _execute_tool_batches: batch {i}, concurrent={batch['concurrent']}, items={len(batch['items'])}")
            if oai_context_break or a.abort_requested():
                a.mark_aborted()
                for ct in batch["items"]:
                    if ct["allowed"]:
                        cancel_result = "Action cancelled: user abort."
                        a.publish_tool_result_event(ct["tc"]["id"], ct["fn"], cancel_result, "cancelled")
                        a.append_tool_message(ct["tc"]["id"], cancel_result, ct["fn"])
                for remaining_batch in oai_batches[oai_batches.index(batch) + 1:]:
                    for ct in remaining_batch["items"]:
                        if ct["allowed"]:
                            cancel_result = "Action cancelled: user abort."
                            a.publish_tool_result_event(ct["tc"]["id"], ct["fn"], cancel_result, "cancelled")
                            a.append_tool_message(ct["tc"]["id"], cancel_result, ct["fn"])
                break

            if batch["concurrent"]:
                await self._execute_concurrent_batch(batch["items"])
            else:
                oai_context_break = await self._execute_sequential_batch(batch["items"])

    async def _execute_concurrent_batch(self, items: list[dict]) -> None:
        """并发执行工具批次。"""
        from agents.observability.trace import trace_event

        a = self._agent

        async def _run_oai_safe(ct_item: dict) -> tuple[dict, str]:
            raw = await a.execute_tool_call(ct_item["fn"], ct_item["inp"])
            raw = _safe_utf8_text(raw)
            res = a.persist_large_result(ct_item["fn"], raw)
            a.publish_tool_result_event(ct_item["tc"]["id"], ct_item["fn"], res, "ok")
            return ct_item, res

        results = await asyncio.gather(*[_run_oai_safe(ct) for ct in items])
        for ct_item, res in results:
            a.record_tool_outcome(ct_item["fn"], not a.looks_like_tool_failure(ct_item["fn"], "", res))
            
            # 检查工具调用警告
            warning_result = check_tool_warnings(self._tool_tracker, ct_item["fn"], ct_item["inp"])
            if warning_result["warnings"]:
                res = res + "\n\n" + "\n".join(warning_result["warnings"])
            
            # 走歪路检测：警告后仍重复调用，自动创建 bad case
            if warning_result["going_off_track"]:
                self._auto_mark_bad_case(
                    signal_type="tool_repeat",
                    diagnosis={"tool": ct_item["fn"], "args": ct_item["inp"], "reason": "警告后仍重复调用"}
                )
            
            # 循环 bad case 检测：警告后仍循环，自动创建 bad case
            if warning_result["cycle_bad_case"]:
                self._auto_mark_bad_case(
                    signal_type="tool_cycle",
                    diagnosis={"tool": ct_item["fn"], "args": ct_item["inp"], "reason": "警告后仍循环"}
                )
            
            # 硬兜底：达到10次强行停止
            if warning_result["force_stop"]:
                stop_info = warning_result["force_stop_info"]
                force_stop_msg = f"\n\n⚠️ 硬兜底：工具 {stop_info['tool']} 已调用 {stop_info['count']} 次，强行停止。请检查任务是否合理，或提供更多上下文。"
                res = res + force_stop_msg
                self._auto_mark_bad_case(
                    signal_type="force_stop",
                    diagnosis={"tool": stop_info["tool"], "count": stop_info["count"], "args": stop_info["args"]}
                )
            
            repeat_warning = a.check_repeat_guard(ct_item["fn"], ct_item["inp"])
            if repeat_warning:
                res = res + "\n\n" + repeat_warning
            a.append_tool_message(ct_item["tc"]["id"], res, ct_item["fn"])

    async def _execute_sequential_batch(self, items: list[dict]) -> bool:
        """顺序执行工具批次。返回是否触发上下文清理。"""
        from agents.observability.trace import trace_event
        from agents.logging import print_info
        import time

        print_info(f"[DEBUG] _execute_sequential_batch: start, {len(items)} tools")
        a = self._agent
        context_break = False

        for i, ct in enumerate(items):
            fn_name = ct["fn"]
            print_info(f"[DEBUG] _execute_sequential_batch: tool {i+1}/{len(items)}: {fn_name}")
            if not ct["allowed"]:
                a.append_tool_message(ct["tc"]["id"], ct["result"], ct["fn"])
                continue

            t0 = time.time()
            print_info(f"[DEBUG] _execute_sequential_batch: calling execute_tool_call for {fn_name}")
            raw = await a.execute_tool_call(ct["fn"], ct["inp"])
            print_info(f"[DEBUG] _execute_sequential_batch: execute_tool_call done for {fn_name}, took {time.time()-t0:.2f}s")
            raw = _safe_utf8_text(raw)
            res = a.persist_large_result(ct["fn"], raw)
            a.publish_tool_result_event(ct["tc"]["id"], ct["fn"], res, "ok")
            a.record_tool_outcome(ct["fn"], not a.looks_like_tool_failure(ct["fn"], raw, res))
            print_info(f"[DEBUG] _execute_sequential_batch: tool {fn_name} completed")

            if a.context_cleared:
                a.clear_context_flag()
                a.append_user_message(res)
                context_break = True
                break

            # 检查工具调用警告
            warning_result = check_tool_warnings(self._tool_tracker, ct["fn"], ct["inp"])
            if warning_result["warnings"]:
                res = res + "\n\n" + "\n".join(warning_result["warnings"])

            # 走歪路检测：警告后仍重复调用，自动创建 bad case
            if warning_result["going_off_track"]:
                self._auto_mark_bad_case(
                    signal_type="tool_repeat",
                    diagnosis={"tool": ct["fn"], "args": ct["inp"], "reason": "警告后仍重复调用"}
                )

            # 循环 bad case 检测：警告后仍循环，自动创建 bad case
            if warning_result["cycle_bad_case"]:
                self._auto_mark_bad_case(
                    signal_type="tool_cycle",
                    diagnosis={"tool": ct["fn"], "args": ct["inp"], "reason": "警告后仍循环"}
                )

            # 硬兜底：达到10次强行停止
            if warning_result["force_stop"]:
                stop_info = warning_result["force_stop_info"]
                force_stop_msg = f"\n\n⚠️ 硬兜底：工具 {stop_info['tool']} 已调用 {stop_info['count']} 次，强行停止。请检查任务是否合理，或提供更多上下文。"
                res = res + force_stop_msg
                self._auto_mark_bad_case(
                    signal_type="force_stop",
                    diagnosis={"tool": stop_info["tool"], "count": stop_info["count"], "args": stop_info["args"]}
                )

            repeat_warning = a.check_repeat_guard(ct["fn"], ct["inp"])
            if repeat_warning:
                res = res + "\n\n" + repeat_warning

            a.append_tool_message(ct["tc"]["id"], res, ct["fn"])

        return context_break

    async def call_model_stream(self) -> dict:
        """流式模型调用。"""
        from agents.observability.trace import trace_event, trace_span
        from agents.agent import _with_retry, ContentLevelError

        a = self._agent
        _model_t0 = time.time()

        with trace_span("model_call", model=a.model, provider="openai") as span:

            async def _do():
                _asm_t0 = time.perf_counter()

                _t1 = time.perf_counter()
                tool_defs = get_active_tool_definitions(a.tools)
                _tool_defs_ms = (time.perf_counter() - _t1) * 1000

                _t2 = time.perf_counter()
                raw_messages = a.messages
                _msg_history_ms = (time.perf_counter() - _t2) * 1000

                _t3 = time.perf_counter()
                sanitized_messages = _sanitize_for_utf8(raw_messages)
                _sanitize_msgs_ms = (time.perf_counter() - _t3) * 1000

                create_params = {
                    "model": a.model,
                    "messages": sanitized_messages,
                    "stream": True,
                    "stream_options": {"include_usage": True},
                }
                if tool_defs:
                    _t4 = time.perf_counter()
                    openai_tools = _to_openai_tools(tool_defs)
                    _convert_tools_ms = (time.perf_counter() - _t4) * 1000

                    _t5 = time.perf_counter()
                    create_params["tools"] = _sanitize_for_utf8(openai_tools)
                    _sanitize_tools_ms = (time.perf_counter() - _t5) * 1000
                else:
                    _convert_tools_ms = 0
                    _sanitize_tools_ms = 0

                _assembly_ms = (time.perf_counter() - _asm_t0) * 1000

                _msg_count = len(raw_messages)
                _msg_chars = sum(len(str(m.get("content", ""))) for m in raw_messages)
                _tool_count = len(tool_defs)

                stream = await a.openai_client.chat.completions.create(**create_params)

                content = ""
                if not a.is_sub_agent:
                    pass
                tool_calls: dict[int, dict] = {}
                finish_reason = ""
                usage = None

                stream_idle_timeout = int(os.environ.get("MYCODE_STREAM_IDLE_TIMEOUT", "60"))
                chunk_iter = stream.__aiter__()
                while True:
                    try:
                        chunk = await asyncio.wait_for(chunk_iter.__anext__(), timeout=stream_idle_timeout)
                    except asyncio.TimeoutError:
                        raise ContentLevelError(f"stream_idle_timeout after {stream_idle_timeout}s")
                    except StopAsyncIteration:
                        break

                    if a.abort_requested():
                        a.mark_aborted()
                        break
                    
                    if chunk.usage:
                        usage = {
                            "prompt_tokens": chunk.usage.prompt_tokens,
                            "completion_tokens": chunk.usage.completion_tokens,
                            "cached_tokens": chunk.usage.prompt_tokens_details.cached_tokens if chunk.usage.prompt_tokens_details else 0,
                        }

                    if not chunk.choices:
                        continue
                    delta = chunk.choices[0].delta

                    reasoning = getattr(delta, "reasoning_content", None)
                    if reasoning:
                        a.append_thinking_text(reasoning)
                        # 流式事件只发送 SSE，不持久化到事件日志
                        event_data = {"content": reasoning, "turn": a._current_turn, "step": a._current_step}
                        if a.current_sub_agent_id:
                            event_data["sub_agent_id"] = a.current_sub_agent_id
                        a.session.append("thinking", event_data)

                    if delta and delta.content:
                        a.emit_text(delta.content)
                        content += _safe_utf8_text(delta.content)
                        # Debug for title agent
                        if a.is_sub_agent and a._custom_system_prompt and "标题" in a._custom_system_prompt:
                            import sys
                            print(f"[TITLE-DEBUG] delta.content='{delta.content}'", file=sys.stderr)

                    if delta and delta.tool_calls:
                        for tc in delta.tool_calls:
                            existing = tool_calls.get(tc.index)
                            if existing:
                                if tc.function and tc.function.arguments:
                                    existing["arguments"] += _safe_utf8_text(tc.function.arguments)
                            else:
                                tool_calls[tc.index] = {
                                    "id": _safe_utf8_text(tc.id or ""),
                                    "name": _safe_utf8_text((tc.function.name if tc.function else "") or ""),
                                    "arguments": _safe_utf8_text((tc.function.arguments if tc.function else "") or ""),
                                }

                    if chunk.choices[0].finish_reason:
                        finish_reason = chunk.choices[0].finish_reason

                assembled = None
                if tool_calls:
                    assembled = [
                        {"id": tc["id"], "type": "function", "function": {"name": tc["name"], "arguments": tc["arguments"]}}
                        for _, tc in sorted(tool_calls.items())
                    ]
                    assembled = [tc for tc in assembled if tc["id"] and tc["function"]["name"]]

                thinking_content = a.get_thinking_content()

                if not content and not assembled and not finish_reason:
                    raise ContentLevelError("empty_response")
                if not content and not assembled and finish_reason == "length":
                    raise ContentLevelError("truncated_response")
                
                # Debug: log content for title agent
                if a.is_sub_agent and a._custom_system_prompt and "标题" in a._custom_system_prompt:
                    import sys
                    print(f"[TITLE-DEBUG] content='{content}', thinking='{thinking_content[:100] if thinking_content else None}...'", file=sys.stderr)
                
                return {
                    "choices": [{
                        "message": {
                            "role": "assistant",
                            "content": content or None,
                            "tool_calls": assembled,
                            "thinking": thinking_content,
                        },
                        "finish_reason": finish_reason or "stop",
                    }],
                    "usage": usage or {"prompt_tokens": 0, "completion_tokens": 0},
                    "_assembly_metrics": {
                        "assembly_ms": round(_assembly_ms, 2),
                        "tool_defs_ms": round(_tool_defs_ms, 2),
                        "msg_history_ms": round(_msg_history_ms, 2),
                        "sanitize_msgs_ms": round(_sanitize_msgs_ms, 2),
                        "convert_tools_ms": round(_convert_tools_ms, 2),
                        "sanitize_tools_ms": round(_sanitize_tools_ms, 2),
                        "msg_count": _msg_count,
                        "msg_chars": _msg_chars,
                        "tool_count": _tool_count,
                    },
                }

            try:
                model_timeout = int(os.environ.get("MYCODE_MODEL_TIMEOUT", "120"))
                result = await asyncio.wait_for(_with_retry(_do), timeout=model_timeout)
                usage = result.get("usage", {}) if isinstance(result, dict) else {}
                input_tokens = usage.get("prompt_tokens", 0)
                output_tokens = usage.get("completion_tokens", 0)
                cached_tokens = usage.get("cached_tokens", 0)
                duration_s = round(time.time() - _model_t0, 2)
                # Langfuse 官方 usage 映射（llm.token_count.*），驱动 UI 成本/Token 统计
                span.set_attribute("llm.token_count.prompt", input_tokens)
                span.set_attribute("llm.token_count.completion", output_tokens)
                span.set_attribute("llm.token_count.total", input_tokens + output_tokens)
                span.set_attribute("langfuse.observation.metadata.cached_tokens", cached_tokens)
                span.set_attribute(
                    "langfuse.observation.metadata.cache_hit_rate",
                    round(cached_tokens / input_tokens, 3) if input_tokens else 0.0,
                )
                span.set_attribute("duration_s", duration_s)
                span.set_attribute("success", True)

                asm = result.pop("_assembly_metrics", None) if isinstance(result, dict) else None
                if asm:
                    span.set_attribute("assembly.total_ms", asm["assembly_ms"])
                    span.set_attribute("assembly.tool_defs_ms", asm["tool_defs_ms"])
                    span.set_attribute("assembly.msg_history_ms", asm["msg_history_ms"])
                    span.set_attribute("assembly.sanitize_msgs_ms", asm["sanitize_msgs_ms"])
                    span.set_attribute("assembly.convert_tools_ms", asm["convert_tools_ms"])
                    span.set_attribute("assembly.sanitize_tools_ms", asm["sanitize_tools_ms"])
                    span.set_attribute("assembly.msg_count", asm["msg_count"])
                    span.set_attribute("assembly.msg_chars", asm["msg_chars"])
                    span.set_attribute("assembly.tool_count", asm["tool_count"])
                
                from agents.observability.cost_tracker import record_tokens
                record_tokens(a.model, input_tokens, output_tokens, cached_tokens)
                
                return result
            except asyncio.TimeoutError:
                duration_s = round(time.time() - _model_t0, 2)
                span.set_attribute("timeout_s", model_timeout)
                span.set_attribute("duration_s", duration_s)
                span.record_error(TimeoutError(f"Model call timed out after {model_timeout}s"))
                raise TimeoutError(f"Model call timed out after {model_timeout}s")
            except Exception as e:
                span.record_error(e)
                raise
