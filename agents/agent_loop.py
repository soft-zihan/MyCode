"""Agent 推理循环。

负责与 LLM 的交互循环：发送消息、接收响应、处理工具调用。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agents.tools.registry import CONCURRENCY_SAFE_TOOLS, get_active_tool_definitions
from agents.observability.tool_tracker import ToolCallTracker, check_tool_warnings

if TYPE_CHECKING:
    from agents.agent import Agent


def _capture_file_snapshot(file_path: str) -> dict | None:
    """Capture file content before/after modification for Code Review."""
    try:
        from agents.tools.runtime import get_runtime, DockerRuntime
        rt = get_runtime()
        if isinstance(rt, DockerRuntime):
            abs_path = file_path
            if not abs_path.startswith("/"):
                abs_path = f"{rt.workdir}/{abs_path}"
        else:
            from agents.tools import resolve_tool_path
            abs_path = str(resolve_tool_path(file_path, must_exist=False).resolve())
        
        path = Path(abs_path)
        if path.exists():
            content = path.read_text(encoding="utf-8", errors="replace")
            return {"file_path": file_path, "content": content, "is_new": False}
        else:
            return {"file_path": file_path, "content": "", "is_new": True}
    except Exception:
        return None


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


_TRACE_TEXT_LIMIT = int(os.environ.get("MYCODE_TRACE_TEXT_LIMIT", "2000"))
_TRACE_MESSAGE_LIMIT = int(os.environ.get("MYCODE_TRACE_MESSAGE_LIMIT", "50"))
_TRACE_PAYLOAD_LIMIT = int(os.environ.get("MYCODE_TRACE_PAYLOAD_LIMIT", "100000"))


def _truncate_trace_text(value: Any, limit: int = _TRACE_TEXT_LIMIT) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False, default=str)
    return value[:limit]


def _compact_message_for_trace(message: dict[str, Any]) -> dict[str, Any]:
    compact: dict[str, Any] = {"role": message.get("role")}
    content = message.get("content")
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                parts.append(_truncate_trace_text(item.get("text") or item, 500))
            else:
                parts.append(_truncate_trace_text(item, 500))
        compact["content"] = "\n".join(parts)[:_TRACE_TEXT_LIMIT]
    else:
        compact["content"] = _truncate_trace_text(content)

    if message.get("tool_calls"):
        compact["tool_calls"] = [
            {
                "id": tc.get("id"),
                "name": (tc.get("function") or {}).get("name"),
                "arguments": _truncate_trace_text((tc.get("function") or {}).get("arguments"), 500),
            }
            for tc in message["tool_calls"]
            if isinstance(tc, dict)
        ]
    if message.get("tool_call_id"):
        compact["tool_call_id"] = message.get("tool_call_id")
    return compact


def _model_input_for_trace(messages: list[dict[str, Any]], tool_defs: list[dict[str, Any]]) -> str:
    recent = messages[-_TRACE_MESSAGE_LIMIT:]
    payload = {
        "message_count": len(messages),
        "omitted_message_count": max(0, len(messages) - len(recent)),
        "messages": [_compact_message_for_trace(msg) for msg in recent if isinstance(msg, dict)],
        "tools": [tool.get("name") for tool in tool_defs],
    }
    return json.dumps(payload, ensure_ascii=False, default=str)[:_TRACE_PAYLOAD_LIMIT]


def _model_output_for_trace(result: dict[str, Any]) -> str:
    choice = (result.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    tool_calls = message.get("tool_calls") or []
    payload = {
        "finish_reason": choice.get("finish_reason"),
        "content": _truncate_trace_text(message.get("content"), 4000),
        "thinking": _truncate_trace_text(message.get("thinking"), 2000),
        "tool_calls": [
            {
                "id": tc.get("id"),
                "name": (tc.get("function") or {}).get("name"),
                "arguments": _truncate_trace_text((tc.get("function") or {}).get("arguments"), 1000),
            }
            for tc in tool_calls
            if isinstance(tc, dict)
        ],
    }
    return json.dumps(payload, ensure_ascii=False, default=str)[:_TRACE_PAYLOAD_LIMIT]


def _usage_details_for_trace(usage: dict[str, Any]) -> dict[str, int]:
    input_tokens = int(usage.get("prompt_tokens") or 0)
    output_tokens = int(usage.get("completion_tokens") or 0)
    total_tokens = int(usage.get("total_tokens") or 0)
    cached_tokens = int(usage.get("cached_tokens") or 0)
    cached_tokens = min(cached_tokens, input_tokens)
    return {
        "input": input_tokens - cached_tokens,
        "input_cached_tokens": cached_tokens,
        "output": output_tokens,
        "total": total_tokens or (input_tokens + output_tokens),
    }


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

    def _auto_mark_bad_case(self, signal_type: str, diagnosis: dict, tool_name: str | None = None) -> None:
        import json
        import uuid
        from agents.logging import print_error
        from agents.observability.bad_cases import (
            BadCase,
            BadCaseSeverity,
            BadCaseSource,
            BadCaseStatus,
            create_bad_case,
        )

        diagnosis_with_context = dict(diagnosis)
        diagnosis_with_context["trace_id"] = getattr(self._agent, "_current_trace_id", None)
        diagnosis_with_context["session_id"] = self._agent.session_id
        reason = signal_type
        if tool_name:
            reason = f"{signal_type}:{tool_name}"

        try:
            create_bad_case(BadCase(
                id=uuid.uuid4().hex,
                session_id=self._agent.session_id,
                source=BadCaseSource.AUTO_DETECT,
                status=BadCaseStatus.PENDING,
                severity=BadCaseSeverity.MEDIUM,
                turn_number=self._agent._current_turn,
                step_number=self._agent._current_step,
                tool_name=tool_name,
                signal_type=signal_type,
                reason=reason,
                comment=json.dumps(diagnosis_with_context, ensure_ascii=False, default=str),
            ))
        except Exception as exc:
            print_error(f"[bad_case] auto mark failed: {type(exc).__name__}: {exc}")

    async def run(self, user_message: str) -> None:
        """主推理循环入口。"""
        await self._prepare_turn(user_message)
        
        while True:
            if self._agent.abort_requested():
                self._agent.mark_aborted()
                break

            await self._consume_wiki_prefetch()

            self._agent.session.append("step/start", {
                "turn": self._agent._current_turn,
                "step": self._agent._current_step + 1,
            })
            self._agent._current_step += 1

            response = await self.call_model_stream()

            choice = response.get("choices", [{}])[0] if response.get("choices") else {}
            message = choice.get("message", {})
            
            thinking_content = message.get("thinking")
            content = message.get("content") or ""
            tool_calls = message.get("tool_calls")
            
            assistant_event = self._agent.session.append("assistant_message", {
                "turn": self._agent._current_turn,
                "step": self._agent._current_step,
                "thinking": thinking_content,
                "content": content,
                "tool_calls": tool_calls,
            })
            self._agent.mark_last_usage_position(int(assistant_event.get("seq", -1)))
            self._update_token_stats(response)

            self._agent.session.append("step/end", {
                "turn": self._agent._current_turn,
                "step": self._agent._current_step,
            })

            if not tool_calls:
                await self._finalize_text_response()
                break

            self._agent.increment_turns()
            budget = self._agent.check_budget()
            if budget["exceeded"]:
                from agents.logging import print_info
                print_info(f"Budget exceeded: {budget['reason']}")
                break

            guard_stop = await self._handle_tool_calls(tool_calls)
            if guard_stop:
                self._finalize_loop_guard_stop()
                break

            if self._agent.tool_budget_exceeded():
                await self._finalize_tool_budget()
                break

            self._agent.clear_context_flag()
            self._agent.refresh_runtime_system_prompt()

    async def _prepare_turn(self, user_message: str) -> None:
        """准备轮次：清理消息、重置状态、创建快照。"""
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
                a.start_wiki_prefetch(user_message, sq)

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

        usage = response.get("usage") or {}
        if not usage:
            return

        input_tokens = int(usage.get("prompt_tokens") or 0)
        output_tokens = int(usage.get("completion_tokens") or 0)
        total_tokens = int(usage.get("total_tokens") or 0) or (input_tokens + output_tokens)
        if total_tokens <= 0:
            logging.warning("model usage returned zero tokens: model=%s", a.model)

        a.add_input_tokens(input_tokens)
        a.add_output_tokens(output_tokens)
        a.set_last_usage_tokens(input_tokens, total_tokens)

        if not a.is_sub_agent:
            cached_tokens = int(usage.get("cached_tokens") or 0)
            a.total_cached_tokens += cached_tokens
            asm = response.get("_assembly_metrics", {})
            a.session.append("stats", {
                "input_tokens": a.total_input_tokens,
                "output_tokens": a.total_output_tokens,
                "cached_tokens": cached_tokens,
                "total_cached_tokens": a.total_cached_tokens,
                "context_window": a.context_window,
                "effective_window": a.effective_window,
                "last_input_token_count": a.last_input_token_count,
                "last_total_token_count": a.last_total_token_count,
                "estimated_context_tokens": a.estimated_context_tokens,
                "system_chars": asm.get("system_chars", 0),
                "user_chars": asm.get("user_chars", 0),
                "assistant_chars": asm.get("assistant_chars", 0),
                "tool_result_chars": asm.get("tool_result_chars", 0),
                "tool_count": asm.get("tool_count", 0),
                "tool_result_by_name": asm.get("tool_result_by_name", {}),
                "system_base_chars": asm.get("system_base_chars", 0),
                "system_claude_md_chars": asm.get("system_claude_md_chars", 0),
                "system_agents_md_chars": asm.get("system_agents_md_chars", 0),
                "system_skills_chars": asm.get("system_skills_chars", 0),
                "system_wiki_chars": asm.get("system_wiki_chars", 0),
                "system_agents_chars": asm.get("system_agents_chars", 0),
                "system_workspace_chars": asm.get("system_workspace_chars", 0),
                "is_plan_mode": asm.get("is_plan_mode", False),
                "plan_mode_chars": asm.get("plan_mode_chars", 0),
            })

    async def _finalize_text_response(self) -> None:
        """完成文本响应：刷新 markdown、打印成本。"""
        pass

    def _finalize_loop_guard_stop(self) -> None:
        a = self._agent
        reason = a._loop_guard_stop_reason or "tool loop guard"
        message = (
            f"⚠️ Loop guard stopped this turn: {reason}\n\n"
            "The previous tool strategy was not making sufficient progress. "
            "Review the latest tool results, choose a different decomposition or tool, "
            "or finish with the evidence already collected."
        )
        if a._turn_output_buffer is not None:
            a._turn_output_buffer.append(message)
        a.session.append("assistant_message", {
            "turn": a._current_turn,
            "step": a._current_step,
            "content": message,
        })

    async def _finalize_tool_budget(self) -> None:
        a = self._agent
        reason = (
            f"Tool call budget exceeded: {a._tool_call_count}/{a.max_tool_calls}"
            if a.max_tool_calls is not None
            else "Tool call budget exceeded"
        )
        a._tool_budget_stop_reason = "tool_budget_exceeded"
        instruction = (
            f"{reason}. Do not call any more tools. "
            "Summarize the evidence already collected, state which subtasks are verified, "
            "which remain uncertain, and give the best supported final result now."
        )
        a.session.append("step/start", {
            "turn": a._current_turn,
            "step": a._current_step + 1,
            "reason": "tool_budget_exceeded",
        })
        a._current_step += 1
        a.append_user_message(instruction)

        response = await self.call_model_stream(tools_enabled=False)
        choice = response.get("choices", [{}])[0] if response.get("choices") else {}
        message = choice.get("message", {})
        assistant_event = a.session.append("assistant_message", {
            "turn": a._current_turn,
            "step": a._current_step,
            "thinking": message.get("thinking"),
            "content": message.get("content") or "",
            "tool_calls": None,
        })
        a.mark_last_usage_position(int(assistant_event.get("seq", -1)))
        self._update_token_stats(response)
        a.session.append("step/end", {
            "turn": a._current_turn,
            "step": a._current_step,
            "reason": "tool_budget_exceeded",
        })

    async def _handle_tool_calls(self, tool_calls: list[dict]) -> bool:
        """处理工具调用：权限检查、执行、结果收集。"""
        from agents.tools.permissions import check_permission
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
        guard_stop = await self._execute_tool_batches(oai_checked)
        print_info(f"[DEBUG] _handle_tool_calls: done, guard_stop={guard_stop}")
        return guard_stop

    async def _execute_tool_batches(self, oai_checked: list[dict]) -> bool:
        """执行工具批次：并发安全工具并行执行，其他顺序执行。返回是否触发 loop guard stop。"""
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
        guard_stop = False
        guard_reason: str | None = None
        try:
            for i, batch in enumerate(oai_batches):
                print_info(f"[DEBUG] _execute_tool_batches: batch {i}, concurrent={batch['concurrent']}, items={len(batch['items'])}")
                if oai_context_break or guard_stop or a.abort_requested():
                    aborted = a.abort_requested()
                    if aborted:
                        a.mark_aborted()
                    cancel_result = (
                        "Action cancelled: user abort."
                        if aborted
                        else f"Action cancelled: loop guard stopped turn ({guard_reason or 'tool loop'})."
                    )
                    cancel_status = "cancelled" if aborted else "error"
                    cancel_outcome = "cancelled" if aborted else "blocked"
                    for remaining_batch in oai_batches[i:]:
                        for ct in remaining_batch["items"]:
                            if ct["allowed"]:
                                a.publish_tool_result_event(
                                    ct["tc"]["id"],
                                    ct["fn"],
                                    cancel_result,
                                    cancel_status,
                                    outcome=cancel_outcome,
                                    metadata={"reason": guard_reason} if guard_reason else None,
                                )
                                a.append_tool_message(ct["tc"]["id"], cancel_result, ct["fn"])
                    break

                if batch["concurrent"]:
                    guard_stop, guard_reason = await self._execute_concurrent_batch(batch["items"])
                else:
                    oai_context_break, guard_stop, guard_reason = await self._execute_sequential_batch(batch["items"])
                if guard_stop:
                    a._loop_guard_stop_reason = guard_reason or "tool_loop"
        finally:
            self._tool_tracker.clear_pending_near_duplicates()

        return guard_stop

    def _publish_blocked_tool_result(self, ct: dict, decision) -> None:
        a = self._agent
        message = decision.message or "Loop guard blocked this repeated tool call."
        a.publish_tool_result_event(
            ct["tc"]["id"],
            ct["fn"],
            message,
            "error",
            outcome="blocked",
            metadata=decision.metadata,
        )
        a.append_tool_message(ct["tc"]["id"], message, ct["fn"])
        self._auto_mark_bad_case(
            signal_type="tool_loop_blocked",
            diagnosis={
                "tool": ct["fn"],
                "args": ct["inp"],
                "reason": decision.reason,
                "verdict": decision.verdict,
                **(decision.metadata or {}),
            },
            tool_name=ct["fn"],
        )

    def _apply_warning_result(self, ct: dict, res: str, warning_result: dict) -> tuple[str, bool, str | None]:
        a = self._agent
        if warning_result["warnings"]:
            res = res + "\n\n" + "\n".join(warning_result["warnings"])

        if warning_result["going_off_track"]:
            self._auto_mark_bad_case(
                signal_type="tool_repeat",
                diagnosis={"tool": ct["fn"], "args": ct["inp"], "reason": "警告后仍重复调用"},
                tool_name=ct["fn"],
            )

        if warning_result["cycle_bad_case"]:
            self._auto_mark_bad_case(
                signal_type="tool_cycle",
                diagnosis={"tool": ct["fn"], "args": ct["inp"], "reason": "警告后仍循环"},
                tool_name=ct["fn"],
            )

        stop_reason: str | None = None
        if warning_result["force_stop"]:
            stop_info = warning_result.get("force_stop_info") or {}
            signal_type = "tool_loop_blocked" if warning_result.get("blocked") else "force_stop"
            stop_reason = stop_info.get("reason") or warning_result.get("verdict") or "force_stop"
            force_stop_msg = (
                f"\n\n⚠️ 硬兜底：工具 {stop_info.get('tool', ct['fn'])} 触发 {stop_reason}，"
                f"count={stop_info.get('count')}，outcome={stop_info.get('outcome') or warning_result.get('outcome')}。"
                "本轮将停止，请检查任务分解、工具选择或已有证据。"
            )
            res = res + force_stop_msg
            self._auto_mark_bad_case(
                signal_type=signal_type,
                diagnosis={
                    "tool": stop_info.get("tool", ct["fn"]),
                    "count": stop_info.get("count"),
                    "args": stop_info.get("args", ct["inp"]),
                    "reason": stop_reason,
                    "guard": stop_info.get("guard"),
                },
                tool_name=stop_info.get("tool", ct["fn"]),
            )

        repeat_warning = a.check_repeat_guard(ct["fn"], ct["inp"])
        if repeat_warning:
            res = res + "\n\n" + repeat_warning

        return res, warning_result["force_stop"], stop_reason

    async def _execute_concurrent_batch(self, items: list[dict]) -> tuple[bool, str | None]:
        """并发执行工具批次。"""
        a = self._agent
        allowed_items: list[dict] = []
        guard_stop = False
        guard_reason: str | None = None

        for ct in items:
            decision = self._tool_tracker.precheck(ct["fn"], ct["inp"])
            if decision.action == "block":
                self._publish_blocked_tool_result(ct, decision)
                guard_stop = True
                guard_reason = guard_reason or decision.reason
            else:
                allowed_items.append(ct)

        if guard_stop:
            for ct in allowed_items:
                cancel_result = f"Action cancelled: loop guard stopped turn ({guard_reason or 'tool loop'})."
                a.publish_tool_result_event(ct["tc"]["id"], ct["fn"], cancel_result, "error", outcome="blocked", metadata={"reason": guard_reason})
                a.append_tool_message(ct["tc"]["id"], cancel_result, ct["fn"])
            return guard_stop, guard_reason

        async def _run_oai_safe(ct_item: dict) -> tuple[dict, Any, str, dict | None]:
            pre_snapshot = None
            if ct_item["fn"] in ("write_file", "edit_file"):
                pre_snapshot = _capture_file_snapshot(ct_item["inp"].get("file_path", ""))
            
            result = await a.execute_tool_call(ct_item["fn"], ct_item["inp"])
            raw = _safe_utf8_text(result.text)
            res = a.persist_large_result(ct_item["fn"], raw)
            
            post_snapshot = None
            if ct_item["fn"] in ("write_file", "edit_file") and pre_snapshot:
                post_snapshot = _capture_file_snapshot(ct_item["inp"].get("file_path", ""))
            
            file_snapshot = None
            if pre_snapshot and post_snapshot:
                file_snapshot = {
                    "file_path": pre_snapshot["file_path"],
                    "is_new": pre_snapshot["is_new"],
                    "old_content": pre_snapshot["content"],
                    "new_content": post_snapshot["content"],
                }
            return ct_item, result, res, file_snapshot

        results = await asyncio.gather(*[_run_oai_safe(ct) for ct in allowed_items])
        for ct_item, result, res, file_snapshot in results:
            text_failure = a.looks_like_tool_failure(ct_item["fn"], result.text, res)
            success = result.status == "ok" and result.outcome == "success" and not text_failure
            outcome = result.outcome if result.outcome != "success" else ("error" if text_failure else "success")
            metadata = dict(result.metadata or {})
            metadata["text_failure"] = text_failure
            a.publish_tool_result_event(
                ct_item["tc"]["id"],
                ct_item["fn"],
                res,
                result.status,
                snapshot=file_snapshot,
                outcome=outcome,
                metadata=metadata,
            )
            a.record_tool_outcome(ct_item["fn"], success)
            
            warning_result = check_tool_warnings(
                self._tool_tracker,
                ct_item["fn"],
                ct_item["inp"],
                success=success,
                outcome=outcome,
                metadata=metadata,
            )
            res, force_stop, stop_reason = self._apply_warning_result(ct_item, res, warning_result)
            if force_stop:
                guard_stop = True
                guard_reason = guard_reason or stop_reason
            a.append_tool_message(ct_item["tc"]["id"], res, ct_item["fn"])

        return guard_stop, guard_reason

    async def _execute_sequential_batch(self, items: list[dict]) -> tuple[bool, bool, str | None]:
        """顺序执行工具批次。返回 (是否触发上下文清理, 是否触发 loop guard, guard reason)。"""
        from agents.logging import print_info
        import time

        print_info(f"[DEBUG] _execute_sequential_batch: start, {len(items)} tools")
        a = self._agent
        context_break = False
        guard_stop = False
        guard_reason: str | None = None

        for i, ct in enumerate(items):
            fn_name = ct["fn"]
            print_info(f"[DEBUG] _execute_sequential_batch: tool {i+1}/{len(items)}: {fn_name}")
            if not ct["allowed"]:
                a.append_tool_message(ct["tc"]["id"], ct["result"], ct["fn"])
                continue

            decision = self._tool_tracker.precheck(fn_name, ct["inp"])
            if decision.action == "block":
                self._publish_blocked_tool_result(ct, decision)
                guard_stop = True
                guard_reason = guard_reason or decision.reason
                break

            t0 = time.time()
            print_info(f"[DEBUG] _execute_sequential_batch: calling execute_tool_call for {fn_name}")
            
            pre_snapshot = None
            if fn_name in ("write_file", "edit_file"):
                pre_snapshot = _capture_file_snapshot(ct["inp"].get("file_path", ""))
            
            result = await a.execute_tool_call(ct["fn"], ct["inp"])
            print_info(f"[DEBUG] _execute_sequential_batch: execute_tool_call done for {fn_name}, took {time.time()-t0:.2f}s")
            raw = _safe_utf8_text(result.text)
            res = a.persist_large_result(ct["fn"], raw)
            
            post_snapshot = None
            if fn_name in ("write_file", "edit_file") and pre_snapshot:
                post_snapshot = _capture_file_snapshot(ct["inp"].get("file_path", ""))
            
            file_snapshot = None
            if pre_snapshot and post_snapshot:
                file_snapshot = {
                    "file_path": pre_snapshot["file_path"],
                    "is_new": pre_snapshot["is_new"],
                    "old_content": pre_snapshot["content"],
                    "new_content": post_snapshot["content"],
                }
            
            text_failure = a.looks_like_tool_failure(ct["fn"], result.text, res)
            success = result.status == "ok" and result.outcome == "success" and not text_failure
            outcome = result.outcome if result.outcome != "success" else ("error" if text_failure else "success")
            metadata = dict(result.metadata or {})
            metadata["text_failure"] = text_failure
            a.publish_tool_result_event(
                ct["tc"]["id"],
                ct["fn"],
                res,
                result.status,
                snapshot=file_snapshot,
                outcome=outcome,
                metadata=metadata,
            )
            a.record_tool_outcome(ct["fn"], success)
            print_info(f"[DEBUG] _execute_sequential_batch: tool {fn_name} completed")

            if a.context_cleared:
                a.clear_context_flag()
                a.append_user_message(res)
                context_break = True
                break

            warning_result = check_tool_warnings(
                self._tool_tracker,
                ct["fn"],
                ct["inp"],
                success=success,
                outcome=outcome,
                metadata=metadata,
            )
            res, force_stop, stop_reason = self._apply_warning_result(ct, res, warning_result)
            if force_stop:
                guard_stop = True
                guard_reason = guard_reason or stop_reason

            a.append_tool_message(ct["tc"]["id"], res, ct["fn"])
            if guard_stop or context_break:
                break

        if guard_stop or context_break:
            for remaining in items[i + 1:]:
                if remaining["allowed"]:
                    cancel_result = (
                        "Action cancelled: context cleared."
                        if context_break
                        else f"Action cancelled: loop guard stopped turn ({guard_reason or 'tool loop'})."
                    )
                    a.publish_tool_result_event(
                        remaining["tc"]["id"],
                        remaining["fn"],
                        cancel_result,
                        "error" if guard_stop else "cancelled",
                        outcome="blocked" if guard_stop else "cancelled",
                        metadata={"reason": guard_reason} if guard_reason else None,
                    )
                    a.append_tool_message(remaining["tc"]["id"], cancel_result, remaining["fn"])

        return context_break, guard_stop, guard_reason

    async def call_model_stream(self, *, tools_enabled: bool = True) -> dict:
        """流式模型调用。"""
        from agents.observability.trace import trace_span
        from agents.agent import _with_retry, ContentLevelError

        a = self._agent
        _model_t0 = time.time()

        with trace_span(
            "model_call",
            model=a.model,
            metadata={
                "provider": "openai",
                "turn_number": a._current_turn,
                "step_number": a._current_step,
                "is_sub_agent": a.is_sub_agent,
            },
        ) as span:

            async def _do():
                await a.check_and_compact()

                _asm_t0 = time.perf_counter()

                _t1 = time.perf_counter()
                tool_defs = get_active_tool_definitions(a.tools) if tools_enabled else []
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
                if a.thinking is not None and "qwen" in a.model.lower():
                    create_params["extra_body"] = {"enable_thinking": bool(a.thinking)}
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

                span.update(
                    input=_model_input_for_trace(sanitized_messages, tool_defs),
                    model_parameters={
                        "model": a.model,
                        "stream": True,
                        "message_count": _msg_count,
                        "tool_count": _tool_count,
                    },
                )

                # 计算 token breakdown（细粒度）
                _system_chars = 0
                _user_chars = 0
                _assistant_chars = 0
                _tool_result_chars = 0
                # System prompt 各部分
                _system_base_chars = 0
                _system_claude_md_chars = 0
                _system_agents_md_chars = 0
                _system_skills_chars = 0
                _system_wiki_chars = 0
                _system_agents_chars = 0
                _system_workspace_chars = 0
                
                for msg in raw_messages:
                    role = msg.get("role", "")
                    content = msg.get("content", "")
                    if isinstance(content, str):
                        chars = len(content)
                    elif isinstance(content, list):
                        chars = sum(len(item.get("text", "")) for item in content if isinstance(item, dict) and item.get("type") == "text")
                    else:
                        chars = 0
                    if role == "system":
                        _system_chars = chars
                    elif role == "user":
                        _user_chars += chars
                    elif role == "assistant":
                        _assistant_chars += chars
                    elif role == "tool":
                        _tool_result_chars += chars
                
                # 计算 system prompt 各部分（重新构建以获取各部分大小）
                try:
                    from agents.core.prompt import (
                        load_claude_md, load_agents_md, build_skill_descriptions,
                        build_wiki_prompt_section,
                        build_agent_descriptions, build_workspace_structure
                    )
                    _system_claude_md_chars = len(load_claude_md())
                    _system_agents_md_chars = len(load_agents_md())
                    _system_skills_chars = len(build_skill_descriptions())
                    _system_wiki_chars = len(build_wiki_prompt_section())
                    _system_agents_chars = len(build_agent_descriptions())
                    _system_workspace_chars = len(build_workspace_structure())
                    _system_base_chars = _system_chars - _system_claude_md_chars - _system_agents_md_chars - _system_skills_chars - _system_wiki_chars - _system_agents_chars - _system_workspace_chars
                except Exception:
                    _system_base_chars = _system_chars
                
                # 计算 plan mode 的额外 token 占用
                _plan_mode_chars = 0
                _is_plan_mode = a.permission_mode == "plan"
                if _is_plan_mode and hasattr(a, '_plan_mode_manager') and a._plan_mode_manager:
                    try:
                        _plan_mode_chars = len(a._plan_mode_manager.build_plan_mode_prompt())
                    except Exception:
                        _plan_mode_chars = 0

                stream = await a.openai_client.chat.completions.create(**create_params)

                from agents.wiki.citation import CitationStripper, strip_citations
                _citation_stripper = CitationStripper()
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
                        prompt_tokens = int(getattr(chunk.usage, "prompt_tokens", 0) or 0)
                        completion_tokens = int(getattr(chunk.usage, "completion_tokens", 0) or 0)
                        total_tokens = int(getattr(chunk.usage, "total_tokens", 0) or 0)
                        usage = {
                            "prompt_tokens": prompt_tokens,
                            "completion_tokens": completion_tokens,
                            "total_tokens": total_tokens or (prompt_tokens + completion_tokens),
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
                        raw_delta = _safe_utf8_text(delta.content)
                        visible = _citation_stripper.feed(raw_delta)
                        if visible:
                            a.emit_text(visible)
                        content += raw_delta
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

                _citation_tail = _citation_stripper.flush()
                if _citation_tail:
                    a.emit_text(_citation_tail)

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
                
                # 4.1：落盘前剥离 citation（防回灌），命中路径计 usage
                content, _cited_paths = strip_citations(content)
                for _p in _cited_paths:
                    try:
                        from agents.wiki.wiki_manager import increment_usage
                        increment_usage(_p)
                    except Exception as _e:
                        print(f"[wiki_citation] usage update failed for {_p}: {type(_e).__name__}: {_e}")

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
                    "usage": usage or {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
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
                        # Token breakdown
                        "system_chars": _system_chars,
                        "user_chars": _user_chars,
                        "assistant_chars": _assistant_chars,
                        "tool_result_chars": _tool_result_chars,
                        # Tool result breakdown by name
                        "tool_result_by_name": dict(getattr(a, '_tool_result_chars', {})),
                        # System prompt 细粒度
                        "system_base_chars": _system_base_chars,
                        "system_claude_md_chars": _system_claude_md_chars,
                        "system_agents_md_chars": _system_agents_md_chars,
                        "system_skills_chars": _system_skills_chars,
                        "system_wiki_chars": _system_wiki_chars,
                        "system_agents_chars": _system_agents_chars,
                        "system_workspace_chars": _system_workspace_chars,
                        # Plan mode
                        "is_plan_mode": _is_plan_mode,
                        "plan_mode_chars": _plan_mode_chars,
                    },
                }

            try:
                model_timeout = int(os.environ.get("MYCODE_MODEL_TIMEOUT", "120"))
                result = await asyncio.wait_for(_with_retry(_do), timeout=model_timeout)
                usage = result.get("usage", {}) if isinstance(result, dict) else {}
                input_tokens = int(usage.get("prompt_tokens", 0) or 0)
                output_tokens = int(usage.get("completion_tokens", 0) or 0)
                total_tokens = int(usage.get("total_tokens", 0) or 0) or (input_tokens + output_tokens)
                cached_tokens = int(usage.get("cached_tokens", 0) or 0)
                duration_s = round(time.time() - _model_t0, 2)
                span.update(
                    output=_model_output_for_trace(result),
                    usage_details=_usage_details_for_trace(usage),
                    metadata={
                        "input_tokens": input_tokens,
                        "output_tokens": output_tokens,
                        "total_tokens": total_tokens,
                        "cached_tokens": cached_tokens,
                        "cache_hit_rate": round(cached_tokens / input_tokens, 3) if input_tokens else 0.0,
                        "duration_s": duration_s,
                        "success": True,
                    },
                )

                asm = result.get("_assembly_metrics", None) if isinstance(result, dict) else None
                if asm:
                    span.add_metadata(assembly_metrics=asm)

                return result
            except asyncio.TimeoutError:
                duration_s = round(time.time() - _model_t0, 2)
                span.add_metadata(timeout_s=model_timeout, duration_s=duration_s)
                span.record_error(TimeoutError(f"Model call timed out after {model_timeout}s"))
                raise TimeoutError(f"Model call timed out after {model_timeout}s")
            except Exception as e:
                span.add_metadata(duration_s=round(time.time() - _model_t0, 2))
                span.record_error(e)
                raise
