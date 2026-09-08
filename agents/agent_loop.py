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

    async def run(self, user_message: str) -> None:
        """主推理循环入口。"""
        await self._prepare_turn(user_message)
        
        while True:
            if self._agent.abort_requested():
                self._agent.mark_aborted()
                break

            await self._consume_memory_prefetch()

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
        
        只扫描工作目录下的文件，返回文件路径和哈希值的列表。
        """
        files = []
        try:
            cwd = Path.cwd()
            # 扫描工作目录下的文件（排除隐藏目录和常见忽略目录）
            ignore_dirs = {".git", ".venv", "node_modules", "__pycache__", ".mypy_cache", ".pytest_cache"}
            
            for file_path in cwd.rglob("*"):
                if not file_path.is_file():
                    continue
                # 跳过隐藏目录和忽略目录
                if any(part.startswith(".") or part in ignore_dirs for part in file_path.parts):
                    continue
                try:
                    content = file_path.read_bytes()
                    files.append({
                        "path": str(file_path.relative_to(cwd)),
                        "hash": hashlib.md5(content).hexdigest()[:8],
                        "size": len(content),
                    })
                except (OSError, PermissionError):
                    pass
        except Exception:
            pass
        return files

    async def _prepare_turn(self, user_message: str) -> None:
        """准备轮次：清理消息、重置状态、创建快照。"""
        from agents.memory.memory import MemoryPrefetch, start_memory_prefetch
        from agents.core.snapshot_service import SnapshotService
        from pathlib import Path
        
        a = self._agent
        user_message = _safe_utf8_text(user_message)
        clean_message = re.sub(r"\n*<retrieved_skills>.*?</retrieved_skills>\s*", "", user_message, flags=re.DOTALL).strip()
        
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
                    a.append_user_message(injection_text)
                    for m in memories:
                        a.record_memory_surface(m.path, len(m.content.encode()))
            except Exception:
                pass

    def _update_token_stats(self, response: dict) -> None:
        """更新 token 统计并发布流式事件。"""
        a = self._agent
        a.last_api_call_time = time.time()

        if response.get("usage"):
            a.add_input_tokens(response["usage"]["prompt_tokens"])
            a.add_output_tokens(response["usage"]["completion_tokens"])
            a.set_last_input_tokens(response["usage"]["prompt_tokens"])
            
            if not a.is_sub_agent:
                # 流式事件只发送 SSE，不持久化到事件日志
                a.session.append("stats", {
                    "input_tokens": a.total_input_tokens,
                    "output_tokens": a.total_output_tokens,
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

        a = self._agent
        oai_checked: list[dict] = []

        for tc in tool_calls:
            if a.abort_requested():
                a.mark_aborted()
                break

            if tc.get("type") != "function":
                continue

            fn_name = tc["function"]["name"]
            try:
                inp = json.loads(tc["function"]["arguments"])
            except Exception:
                inp = {}

            a.publish_tool_call_event(tc["id"], fn_name, inp)

            perm = check_permission(fn_name, inp, a.permission_mode, a.plan_file_path)

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

        await self._execute_tool_batches(oai_checked)

    async def _execute_tool_batches(self, oai_checked: list[dict]) -> None:
        """执行工具批次：并发安全工具并行执行，其他顺序执行。"""
        from agents.observability.trace import trace_event

        a = self._agent
        oai_batches: list[dict] = []
        
        for ct in oai_checked:
            safe = ct["allowed"] and ct["fn"] in CONCURRENCY_SAFE_TOOLS
            if safe and oai_batches and oai_batches[-1]["concurrent"]:
                oai_batches[-1]["items"].append(ct)
            else:
                oai_batches.append({"concurrent": safe, "items": [ct]})

        oai_context_break = False
        for batch in oai_batches:
            if oai_context_break or a.abort_requested():
                a.mark_aborted()
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
            repeat_warning = a.check_repeat_guard(ct_item["fn"], ct_item["inp"])
            if repeat_warning:
                res = res + "\n\n" + repeat_warning
            a.append_tool_message(ct_item["tc"]["id"], res)

    async def _execute_sequential_batch(self, items: list[dict]) -> bool:
        """顺序执行工具批次。返回是否触发上下文清理。"""
        from agents.observability.trace import trace_event

        a = self._agent
        context_break = False

        for ct in items:
            if not ct["allowed"]:
                a.append_tool_message(ct["tc"]["id"], ct["result"])
                continue

            raw = await a.execute_tool_call(ct["fn"], ct["inp"])
            raw = _safe_utf8_text(raw)
            res = a.persist_large_result(ct["fn"], raw)
            a.publish_tool_result_event(ct["tc"]["id"], ct["fn"], res, "ok")
            a.record_tool_outcome(ct["fn"], not a.looks_like_tool_failure(ct["fn"], raw, res))

            if a.context_cleared:
                a.clear_context_flag()
                a.append_user_message(res)
                context_break = True
                break

            repeat_warning = a.check_repeat_guard(ct["fn"], ct["inp"])
            if repeat_warning:
                res = res + "\n\n" + repeat_warning

            a.append_tool_message(ct["tc"]["id"], res)

        return context_break

    async def call_model_stream(self) -> dict:
        """流式模型调用。"""
        from agents.observability.trace import trace_event, trace_span
        from agents.agent import _with_retry

        a = self._agent
        _model_t0 = time.time()

        with trace_span("model_call", model=a.model, provider="openai") as span:

            async def _do():
                tool_defs = get_active_tool_definitions(a.tools)
                create_params = {
                    "model": a.model,
                    "messages": _sanitize_for_utf8(a.messages),
                    "stream": True,
                    "stream_options": {"include_usage": True},
                }
                if tool_defs:
                    create_params["tools"] = _sanitize_for_utf8(_to_openai_tools(tool_defs))
                stream = await a.openai_client.chat.completions.create(**create_params)

                content = ""
                if not a.is_sub_agent:
                    pass
                tool_calls: dict[int, dict] = {}
                finish_reason = ""
                usage = None

                async for chunk in stream:
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

                thinking_content = a.get_thinking_content()
                
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
                }

            try:
                model_timeout = int(os.environ.get("MYCODE_MODEL_TIMEOUT", "120"))
                result = await asyncio.wait_for(_with_retry(_do), timeout=model_timeout)
                usage = result.get("usage", {}) if isinstance(result, dict) else {}
                input_tokens = usage.get("prompt_tokens", 0)
                output_tokens = usage.get("completion_tokens", 0)
                cached_tokens = usage.get("cached_tokens", 0)
                duration_s = round(time.time() - _model_t0, 2)
                span.set_attribute("input_tokens", input_tokens)
                span.set_attribute("output_tokens", output_tokens)
                span.set_attribute("cached_tokens", cached_tokens)
                span.set_attribute("duration_s", duration_s)
                span.set_attribute("success", True)
                
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
