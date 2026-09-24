"""Turn 级执行协调层（U0 从 Agent.chat/_chat_inner/run_once 迁出）。

职责边界（三层分离，U3 后台化与 U6 崩溃恢复的结构前提）：
- TurnRunner（本模块）：turn 生命周期——workspace/trace 上下文、MCP 懒初始化、
  注入收集、turn 状态重置、任务启动/取消/异常 → turn/start·turn/end 事件、
  turn 收尾（trace 上报 + skill evolution 调度）。
- AgentLoop：step 级循环——模型调用 ↔ 工具批次、steer 队列 drain、预算/守卫。
- ModelCaller：单次 LLM 请求-响应细节。

行为与原 Agent._chat_inner 逐行等价（U0 纪律：不夹带功能变更）。
已知既有缺陷保留（BC-22，待单独修复）：成功路径 turn/end 事件构建发生在
finally 清空 _current_trace_id 之后 → trace_id 恒 None；abort/error 路径
在 except 内构建事件故携带真实 trace_id。
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any

from agents.core.text_sanitization import safe_utf8_text
from agents.logging import print_divider, print_error
from agents.wiki.wiki_manager import list_pending_confirm_entries

if TYPE_CHECKING:
    from agents.agent import Agent


class TurnRunner:
    def __init__(self, agent: "Agent"):
        self._agent = agent

    async def chat(self, user_message: str) -> None:
        """单轮对话入口：workspace 上下文 + trace 上下文 + run_turn。"""
        from agents.core.workspace import set_workspace, reset_workspace
        from agents.observability.trace import trace_context

        a = self._agent
        trace_tags = ["sub-agent"] if a.is_sub_agent else ["main-agent"]
        if a.permission_mode == "plan":
            trace_tags.append("plan-mode")

        _ws_token = set_workspace(a.workspace)
        try:
            with trace_context(
                session_id=a.session_id if not a.is_sub_agent else None,
                trace_name="agent-turn",
                tags=trace_tags,
            ):
                await self.run_turn(user_message)
        finally:
            reset_workspace(_ws_token)

    async def run_turn(self, user_message: str) -> None:
        """原 _chat_inner：turn 生命周期六段编排。"""
        a = self._agent
        print(f"[DEBUG] agent.chat: STARTED - self.session_id = {a.session_id}, is_sub_agent = {a.is_sub_agent}")
        await self._init_mcp_once()

        # D3 hotfix：记忆/wiki 提醒不再拼接 system prompt——改 system[0] 会破坏
        # prefix cache 且在长会话中无限累积（每轮 += 永不还原）。改走既有
        # memory_injection 落盘事件通道：渲染为本轮 user_message 之后的 user
        # 消息，cache 零影响、事件流可追溯（对齐 v2 anti-recall /
        # instruction-state 模式，参见升级方案 §3 表 6）。
        # D11：跨会话记忆收敛 wiki 单源——folded-memory JSON 检索通道已删除
        # （schema 漂移致恒 0 分死通道，事件流已是唯一数据源）。
        a._pending_system_injections = self._collect_pending_injections()

        original_user_message = safe_utf8_text(user_message)
        ready_skill_extraction_window = self._pop_skill_extraction_window(original_user_message)
        self._reset_turn_state()

        from agents.observability.trace import trace_span

        _turn_event_start_seq = a.session.seq
        _turn_t0 = time.time()
        _turn_start_input_tokens = a.total_input_tokens
        _turn_start_output_tokens = a.total_output_tokens
        with trace_span(
            "turn",
            input=user_message[:4000],
            metadata={
                "session_id": a.session_id,
                "model": a.model,
                "workspace": str(a.workspace),
                "permission_mode": a.permission_mode,
                "turn_id": f"{a.session_id}:{a._current_turn}",
                "turn_number": a._current_turn,
                "event_range_start_seq": _turn_event_start_seq,
                "is_sub_agent": a.is_sub_agent,
            },
        ) as turn_span:
            a._current_trace_id = turn_span.get_trace_id()
            a.session.append("turn/start", {
                "turn": a._current_turn,
                "trace_id": a._current_trace_id,
            })
            assistant_text = await self._run_turn_task(user_message, original_user_message, turn_span)
            if assistant_text is None:
                # 异常路径：turn/end 已在 _run_turn_task 内落盘（原实现 return 语义）
                return
            a._turn_output_buffer = None
            a._turn_thinking_buffer = None
            a._turn_event_buffer = None
            self._finalize_turn_events(
                turn_span, assistant_text, _turn_t0,
                _turn_start_input_tokens, _turn_start_output_tokens,
            )
        a._last_assistant_text = assistant_text
        self._schedule_skill_evolution(
            ready_skill_extraction_window, original_user_message, assistant_text
        )

    async def run_once(self, prompt: str) -> dict:
        a = self._agent
        a._output_buffer = []
        prev_in = a.total_input_tokens
        prev_out = a.total_output_tokens
        await self.chat(prompt)
        text = "".join(a._output_buffer)
        a._output_buffer = None
        stop_reason = a._loop_guard_stop_reason or a._tool_budget_stop_reason
        return {
            "text": text,
            "tokens": {
                "input": a.total_input_tokens - prev_in,
                "output": a.total_output_tokens - prev_out
            },
            "stop_reason": stop_reason,
            "tool_budget_exceeded": bool(a._tool_budget_stop_reason),
            "tool_call_count": a._tool_call_count,
            "failed_tool_call_count": a._failed_tool_call_count,
        }

    # ── 六段拆分 ──

    async def _init_mcp_once(self) -> None:
        """段1：MCP 懒初始化（仅主代理、仅一次；失败降级继续）。"""
        a = self._agent
        if a._mcp_initialized or a.is_sub_agent:
            return
        print("[DEBUG] agent.chat: initializing MCP")
        a._mcp_initialized = True
        from agents.observability.trace import trace_span
        with trace_span("mcp.init") as span:
            try:
                await asyncio.wait_for(
                    a._mcp_manager.load_and_connect(),
                    timeout=30.0
                )
                mcp_defs = a._mcp_manager.get_tool_definitions()
                if mcp_defs:
                    from agents.tools.mcp_registry import global_registry
                    for mcp_def in mcp_defs:
                        parts = mcp_def["name"].split("__")
                        if len(parts) >= 3:
                            server_name = parts[1]
                            global_registry.register_mcp(mcp_def, server=server_name)
                    a.tools = a.tools + mcp_defs
                    # U5a：工具指引按工具快照条件生成——MCP 接入是结构性变化，
                    # force 重建一次（字节相同时 setter 不 bump，缓存无损；
                    # 代码图等 MCP 专属指引自此可见）
                    a._refresh_runtime_system_prompt(force=True)
                span.add_metadata(success=True, tool_count=len(mcp_defs) if mcp_defs else 0)
            except asyncio.TimeoutError:
                error = TimeoutError("MCP init timeout (30s)")
                span.update(output="timeout", metadata={"success": False})
                span.record_error(error)
                print_error("MCP init timeout (30s) - continuing without MCP tools")
            except Exception as e:
                span.update(output=str(e)[:2000], metadata={"success": False})
                span.record_error(e)
                print_error(f"MCP init failed: {e}")
        print("[DEBUG] agent.chat: MCP init done")

    def _collect_pending_injections(self) -> list[str]:
        """段2：本轮注入收集（当前仅 wiki 待确认提醒，首轮）。"""
        a = self._agent
        pending_injections: list[str] = []
        if not a.is_sub_agent and a._turn_number == 0:
            try:
                pending = list_pending_confirm_entries()
                if pending:
                    names = ", ".join(e.name for e in pending[:5])
                    pending_injections.append(
                        f"<system-reminder>\n有 {len(pending)} 条待确认的调试经验：{names}。输入 /wiki-confirm 确认或 /wiki-reject 拒绝。\n</system-reminder>"
                    )
            except Exception as e:
                print_error(f"[WARN] wiki pending-confirm check failed: {e}")
        return pending_injections

    def _pop_skill_extraction_window(self, original_user_message: str) -> dict[str, Any] | None:
        """段3a：skill 进化窗口弹出（上一轮积累的对话窗口，本轮结束时判定触发）。"""
        a = self._agent
        a._skill_orchestrator.last_retrieved_skill_reference = None
        if a.is_sub_agent:
            return None
        return a._skill_orchestrator.pop_pending_extraction_window(
            original_user_message, a._tool_error_streak
        )

    def _reset_turn_state(self) -> None:
        """段3b：turn 状态重置（abort 标志/缓冲区/守卫原因/计数器）。"""
        a = self._agent
        a._aborted = False
        a._abort_event.clear()
        a._turn_number += 1
        a._turn_output_buffer = []
        a._turn_thinking_buffer = []
        a._turn_event_buffer = []
        a._loop_guard_stop_reason = None
        a._tool_budget_stop_reason = None
        a._user_message_written_this_turn = False
        a._current_turn += 1

    async def _run_turn_task(self, user_message: str, original_user_message: str, turn_span) -> str | None:
        """段4：任务启动 + 取消/异常处理。返回 assistant_text；异常路径返回 None（turn/end 已落盘）。"""
        a = self._agent
        coro = a._chat_openai(user_message)
        a._current_task = asyncio.create_task(coro)
        try:
            await a._current_task
        except asyncio.CancelledError:
            a._aborted = True
            if not a._user_message_written_this_turn:
                a.session.append("user_message", {"content": original_user_message})
                a._user_message_written_this_turn = True
            turn_span.add_metadata(aborted=True, event_range_end_seq=a.session.seq)
            a.session.append("turn/end", {
                "turn": a._current_turn,
                "reason": "aborted",
                "sub_agent_id": a._current_sub_agent_id,
                "trace_id": a._current_trace_id,
            })
            raise
        except Exception as e:
            import traceback
            print_error(f"[ERROR] {type(e).__name__}: {e}\n{traceback.format_exc()}")
            turn_span.record_error(e)
            turn_span.add_metadata(event_range_end_seq=a.session.seq)
            a.session.append("error", {"message": str(e), "error_type": type(e).__name__, "sub_agent_id": a._current_sub_agent_id})
            a.session.append("turn/end", {
                "turn": a._current_turn,
                "reason": "error",
                "error": str(e),
                "sub_agent_id": a._current_sub_agent_id,
                "trace_id": a._current_trace_id,
            })
            return None
        finally:
            a._current_task = None
            a._current_trace_id = None
        return "".join(a._turn_output_buffer or []).strip()

    def _finalize_turn_events(self, turn_span, assistant_text: str, _turn_t0: float,
                              _turn_start_input_tokens: int, _turn_start_output_tokens: int) -> None:
        """段5：turn/end 事件 + trace 收尾上报。

        BC-22（既有缺陷，行为保留）：本段在 _run_turn_task 的 finally 清空
        _current_trace_id 之后执行 → 成功路径 turn/end 的 trace_id 恒 None。
        """
        a = self._agent
        if a._loop_guard_stop_reason:
            turn_end_reason = "loop_guard"
        elif a._tool_budget_stop_reason:
            turn_end_reason = "budget_exceeded"
        else:
            turn_end_reason = "completed"
        turn_end_event = {
            "turn": a._current_turn,
            "reason": turn_end_reason,
            "sub_agent_id": a._current_sub_agent_id,
            "trace_id": a._current_trace_id,
        }
        if a._loop_guard_stop_reason:
            turn_end_event["loop_guard_reason"] = a._loop_guard_stop_reason
        if a._tool_budget_stop_reason:
            turn_end_event["tool_budget_reason"] = a._tool_budget_stop_reason
            turn_end_event["tool_call_count"] = a._tool_call_count
            turn_end_event["max_tool_calls"] = a.max_tool_calls
        a.session.append("turn/end", turn_end_event)
        turn_span.update(
            output=assistant_text[:20000],
            metadata={
                "event_range_end_seq": a.session.seq,
                "aborted": a._aborted,
                "loop_guard_reason": a._loop_guard_stop_reason,
                "tool_budget_reason": a._tool_budget_stop_reason,
                "tool_call_count": a._tool_call_count,
                "failed_tool_call_count": a._failed_tool_call_count,
                "max_tool_calls": a.max_tool_calls,
                "input_tokens_delta": a.total_input_tokens - _turn_start_input_tokens,
                "output_tokens_delta": a.total_output_tokens - _turn_start_output_tokens,
                "duration_s": round(time.time() - _turn_t0, 2),
            },
        )

    def _schedule_skill_evolution(self, ready_skill_extraction_window: dict[str, Any] | None,
                                  original_user_message: str, assistant_text: str) -> None:
        """段6：skill evolution 触发判定 + 下一轮提取窗口设置（仅主代理、未中止）。"""
        a = self._agent
        if not a.is_sub_agent and not a._aborted:
            a._skill_orchestrator.turns_since_last_evolution += 1
            if ready_skill_extraction_window and a._skill_orchestrator.should_trigger_evolution():
                a._skill_orchestrator.schedule_background_task(
                    a._skill_orchestrator.run_online_skill_evolution(ready_skill_extraction_window),
                    plan_mode=(a.permission_mode == "plan"),
                )
                a._skill_orchestrator.record_evolution_event()

            a._skill_orchestrator.set_pending_extraction_window(
                messages=a._skill_orchestrator.get_recent_dialog_messages(a.session.get_messages_for_llm(), max_messages=8),
                original_user_message=original_user_message,
                assistant_text=assistant_text,
                retrieved_reference=a._skill_orchestrator.last_retrieved_skill_reference,
                tool_error_streak=a._tool_error_streak,
            )
        if not a.is_sub_agent:
            try:
                print_divider()
            except Exception:
                pass
