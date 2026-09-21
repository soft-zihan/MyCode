#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import uuid
from pathlib import Path
from typing import Callable, Awaitable, Any

import openai

from agents.tools.mcp import McpManager
from agents.agent_loop import AgentLoop
from agents.tools.executor import persist_large_result, detect_failure
from agents.wiki.wiki_manager import (
    select_relevant_wiki_entries,
    format_wiki_for_injection,
    build_wiki_prompt_section,
    list_pending_confirm_entries,
    init_wiki_git,
    WikiEntry,
)
from agents.core.model_registry import ModelEndpoint, resolve_agent_endpoint, resolve_side_endpoint
from agents.core.prompt import build_system_prompt
from agents.core.session_memory import (
    FOLD_SESSION_MEMORY_SYSTEM,
    build_folding_user_prompt,
    build_openai_transcript,
    fallback_folded_memory,
    format_folded_memory,
    parse_folded_memory,
)
from agents.core.session import save_folded_session_memory, Session
from agents.core.subagent import get_sub_agent_config
from agents.tools import ToolDef, tool_definitions, execute_tool, CONCURRENCY_SAFE_TOOLS, check_permission, \
    get_active_tool_definitions
from agents.logging import print_info, print_divider, print_assistant_text, print_sub_agent_start, print_sub_agent_end, \
    print_error, print_retry
from agents.plan.plan_mode import PlanModeManager
from agents.tools.dispatcher import ToolDispatcher
from agents.core.context import ContextManager


class ContentLevelError(Exception):
    """模型返回内容级错误（空响应、截断、畸形 tool_call 等）。"""
    pass


def _is_retryable(error: Exception) -> bool:
    status = getattr(error, "status_code", None) or getattr(error, "status", None)
    if status in (429, 503, 529):
        return True
    msg = str(error)
    if "overloaded" in msg or "ECONNRESET" in msg or "ETIMEDOUT" in msg:
        return True
    if isinstance(error, ContentLevelError):
        return True
    return False


def _safe_utf8_text(value: object) -> str:
    return str(value).encode("utf-8", errors="replace").decode("utf-8")


def _sanitize_for_utf8(value: Any) -> Any:
    if isinstance(value, str):
        return _safe_utf8_text(value)
    if isinstance(value, list):
        return [_sanitize_for_utf8(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_sanitize_for_utf8(item) for item in value)
    if isinstance(value, dict):
        return {
            _sanitize_for_utf8(key): _sanitize_for_utf8(item)
            for key, item in value.items()
        }
    return value


async def _with_retry(fn, max_retries: int = 3):
    from agents.core.circuit_breaker import llm_circuit_breaker

    if not llm_circuit_breaker.can_execute():
        raise RuntimeError("LLM API circuit breaker is open, retry later")

    for attempt in range(max_retries + 1):
        try:
            result = await fn()
            llm_circuit_breaker.record_success()
            return result
        except Exception as error:
            llm_circuit_breaker.record_failure()
            if attempt >= max_retries or not _is_retryable(error):
                raise
            delay = min(1000 * (2 ** attempt), 30000) / 1000 + (hash(str(time.time())) % 1000) / 1000
            status = getattr(error, "status_code", None) or getattr(error, "status", None)
            reason = f"HTTP {status}" if status else (getattr(error, "code", None) or "network error")
            print_retry(attempt + 1, max_retries, reason)
            await asyncio.sleep(delay)


def _get_max_output_tokens(model: str) -> int:
    m = model.lower()
    if "opus-4-6" in m:
        return 64000
    if "sonnet-4-6" in m:
        return 32000
    if any(x in m for x in ("opus-4", "sonnet-4", "haiku-4")):
        return 32000
    return 16384


def _to_openai_tools(tools: list[ToolDef]) -> list[dict]:
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


class Agent:
    def __init__(self,
                 *,
                 permission_mode:str="default",
                 model:str="deepseek-chat",
                 api_base: str | None=None,
                 api_key: str | None=None,
                 thinking: bool=False,
                 max_cost_usd: float | None=None,
                 max_turns: int | None=None,
                 max_tool_calls: int | None=None,
                 confirm_fn:Callable[[str], Awaitable[bool]] | None=None,
                 custom_system_prompt: str | None=None,
                 custom_tools: list[ToolDef] | None=None,
                 is_sub_agent: bool=False,
                 parent_abort_event: asyncio.Event | None=None,
                 workspace: Any = None,
                 session_id: str | None=None,
                 options: Any | None=None,):
        if options is not None:
            from agents.core.options import AgentOptions
            if isinstance(options, AgentOptions):
                permission_mode = options.permission_mode
                model = options.model
                api_base = options.api_base
                api_key = options.api_key
                thinking = options.thinking
                max_cost_usd = options.max_cost_usd
                max_turns = options.max_turns
                max_tool_calls = options.max_tool_calls
                confirm_fn = options.confirm_fn
                custom_system_prompt = options.custom_system_prompt
                custom_tools = options.custom_tools
                is_sub_agent = options.is_sub_agent
                parent_abort_event = options.parent_abort_event
                workspace = options.workspace
                session_id = getattr(options, 'session_id', None)

        self.permission_mode = permission_mode
        self.thinking = thinking
        self.model = model
        self.is_sub_agent = is_sub_agent
        self.tools = custom_tools if custom_tools is not None else tool_definitions
        self.max_cost_usd = max_cost_usd
        self.max_turns = max_turns
        self.max_tool_calls = max_tool_calls
        self.confirm_fn = confirm_fn
        self._custom_system_prompt = custom_system_prompt
        self.workspace: Path = Path(workspace).resolve() if workspace else Path.cwd()
        self._api_base = api_base
        self._api_key = api_key
        self._side_client_cache: tuple[tuple, tuple] | None = None
        from agents.config import (
            DEFAULT_AUTO_COMPACT_THRESHOLD,
            DEFAULT_CONTEXT_WINDOW,
            get_endpoint_by_model,
        )
        _ep = get_endpoint_by_model(model)
        self.context_window = _ep.context_window if _ep else DEFAULT_CONTEXT_WINDOW
        self.effective_window = self.context_window - 20000
        self.auto_compact_threshold = _ep.auto_compact_threshold if _ep else DEFAULT_AUTO_COMPACT_THRESHOLD
        self.session_id = session_id or uuid.uuid4().hex[:8]
        self.session_start_time= time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime())

        self.session = Session(self.session_id, origin="sub_agent" if is_sub_agent else None)
        self._current_turn: int = 0
        self._current_step: int = 0
        self._user_message_written_this_turn: bool = False
        self._permission_waiters: dict[str, asyncio.Future] = {}

        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.total_cached_tokens = 0
        self.last_input_token_count = 0
        self.last_total_token_count = 0
        self.last_usage_seq = -1
        self.current_turns = 0
        self.last_api_call_time = 0

        self._last_assistant_text = ""

        from .core.context_compressor import ContextCompressor
        from .core.permission_gate import PermissionGate
        from .core.session_lifecycle import SessionLifecycle
        from .skills.skill_orchestrator import SkillOrchestrator
        self._compressor = ContextCompressor(
            effective_window=self.effective_window,
            tool_fold_threshold=self.auto_compact_threshold,
            wiki_enabled=not self.is_sub_agent,
        )
        self._permission_gate = PermissionGate()
        self._session_lifecycle = SessionLifecycle()
        self._skill_orchestrator = SkillOrchestrator(
            side_query_fn=self._build_side_query,
            permission_mode=self.permission_mode,
            session_id=self.session_id,
            refresh_system_prompt=self._refresh_runtime_system_prompt,
        )

        self._aborted = False
        self._abort_event = asyncio.Event()
        self._parent_abort_event = parent_abort_event
        self._current_task: asyncio.Task | None = None
        self._current_trace_id: str | None = None
        self._confirmed_paths: set[str] = set()

        self._context_cleared: bool = False

        self._thinking_mode = self._resolve_thinking_mode()

        self._output_buffer: list[str] | None=None
        self._turn_output_buffer: list[str] | None = None
        self._turn_thinking_buffer: list[str] | None = None
        self._turn_event_buffer: list[dict] | None = None

        self._current_sub_agent_id: str | None = None

        self._read_file_state: dict[str, float] ={}

        self._mcp_manager = McpManager()
        self._mcp_initialized = False

        self._turn_number = 0

        self._wiki_surfaced_at: dict[str, int] = {}
        self._wiki_prefetch: asyncio.Task | None = None
        self._wiki_prefetch_consumed = False

        self._folded_session_memories: list[dict[str, Any]] = []
        self._fold_last_time: float = 0.0
        self._fold_count: int = 0
        self._tool_error_streak: int = 0
        self._same_tool_repeat_count: int = 0
        self._last_tool_name: str = ""
        self._repeat_chain_key: str = ""
        self._repeat_chain_count: int = 0
        self._loop_guard_stop_reason: str | None = None
        self._tool_budget_stop_reason: str | None = None
        self._tool_result_chars: dict[str, int] = {}  # 每个工具的结果字符数统计
        self._tool_call_count: int = 0
        self._failed_tool_call_count: int = 0

        from .core.workspace import set_workspace, reset_workspace
        _ws_token = set_workspace(self.workspace)
        try:
            self._base_system_prompt = custom_system_prompt or build_system_prompt()

            self._plan_mode_manager = PlanModeManager(
                workspace=self.workspace,
                session_id=self.session_id,
                plan_approval_fn=None,
            )

            if self.permission_mode == "plan":
                self._plan_mode_manager.plan_dir = self._plan_mode_manager.generate_plan_dir()
                self._system_prompt = self._base_system_prompt + self._plan_mode_manager.build_plan_mode_prompt()
                print(f"[DEBUG] Agent.__init__: Entered plan mode. Plan dir: {self._plan_mode_manager.plan_dir}")
            else:
                self._system_prompt = self._base_system_prompt

            self._openai_client = openai.AsyncOpenAI(base_url=api_base, api_key=api_key)

            self._refresh_runtime_system_prompt()
        finally:
            reset_workspace(_ws_token)
        self._loop = AgentLoop(self)

        self._tool_dispatcher = ToolDispatcher(agent_ref=self)
        self._context_manager = ContextManager(agent_ref=self)

    def _resolve_thinking_mode(self) -> str:
        if not self.thinking:
            return "disabled"
        if not self._model_supports_thinking():
            return "disabled"

        if self._model_supports_adaptive_thinking():
            return "adaptive"
        return "enabled"

    def _model_supports_thinking(self) -> bool:
        m = self.model.lower()
        if "claude-3-" in m or "3-5-" in m or "3-7-" in m:
            return False
        if "claude" in m and any(x in m for x in ("opus", "sonnet", "haiku")):
            return True
        return False

    def _model_supports_adaptive_thinking(self) -> bool:
        m = self.model.lower()
        return "opus-4-6" in m or "sonnet-4-6" in m

    @property
    def is_processing(self)->bool:
        return self._current_task is not None and not self._current_task.done()

    def _get_side_client(self):
        endpoint = resolve_side_endpoint(primary=self._primary_endpoint())
        if (endpoint.model == self.model
                and endpoint.base_url == self._api_base):
            return None
        cache_key = (endpoint.model, endpoint.base_url, True)
        cached = getattr(self, "_side_client_cache", None)
        if cached and cached[0] == cache_key:
            return cached[1]
        client = openai.AsyncOpenAI(base_url=endpoint.base_url, api_key=endpoint.api_key)
        result = (client, endpoint.model, True)
        self._side_client_cache = (cache_key, result)
        return result

    def _build_side_query(self, *, max_tokens: int = 256):
        side = self._get_side_client()
        if side is not None:
            client, model, use_openai = side
        elif self._openai_client:
            client, model, use_openai = self._openai_client, self.model, True
        else:
            return None

        async def _sq_openai(system: str, user_message: str) -> str:
            from agents.observability.trace import trace_span

            messages = [
                {"role": "system", "content": system},
                {"role": "user", "content": user_message},
            ]
            normalized_max_tokens = max(1, int(max_tokens))
            input_payload = json.dumps({
                "model": model,
                "max_tokens": normalized_max_tokens,
                "messages": messages,
            }, ensure_ascii=False, default=str)[:4000]

            with trace_span(
                "side_query",
                name=f"side_query.{model}",
                model=model,
                input=input_payload,
                metadata={"max_tokens": normalized_max_tokens},
            ) as span:
                resp = await client.chat.completions.create(
                    model=model,
                    max_tokens=normalized_max_tokens,
                    messages=messages,
                )
                if not resp.choices:
                    logging.warning("side_query returned no OpenAI-compatible choices: model=%s", model)
                    span.update(output="")
                    span.record_error(RuntimeError("side_query returned no choices"))
                    return ""

                choice = resp.choices[0]
                content = choice.message.content or ""
                usage = getattr(resp, "usage", None)
                if usage is not None:
                    input_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
                    output_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
                    total_tokens = int(getattr(usage, "total_tokens", 0) or 0) or (input_tokens + output_tokens)
                    cached_tokens = int(getattr(getattr(usage, "prompt_tokens_details", None), "cached_tokens", 0) or 0)
                    cached_tokens = min(cached_tokens, input_tokens)
                    span.update(usage_details={
                        "input": input_tokens - cached_tokens,
                        "input_cached_tokens": cached_tokens,
                        "output": output_tokens,
                        "total": total_tokens,
                    })

                span.update(output=json.dumps({
                    "content": content[:4000],
                    "finish_reason": getattr(choice, "finish_reason", None),
                }, ensure_ascii=False, default=str))

                if not content.strip():
                    logging.warning(
                        "side_query returned empty OpenAI-compatible response: model=%s finish_reason=%s message=%s",
                        model,
                        getattr(choice, "finish_reason", ""),
                        choice.message,
                    )
                return content
        return _sq_openai

    def abort(self) -> None:
        self._aborted = True
        self._abort_event.set()
        if self._current_task and not self._current_task.done():
            self._current_task.cancel()

    def _abort_requested(self) -> bool:
        if self._aborted:
            return True
        if self._parent_abort_event is not None and self._parent_abort_event.is_set():
            return True
        return False

    def _primary_endpoint(self) -> ModelEndpoint:
        return ModelEndpoint(
            model=self.model,
            base_url=self._api_base,
            api_key=self._api_key,
            use_openai=True,
        )

    def _spawn_sub_agent(
        self,
        *,
        system_prompt: str,
        tools: list[ToolDef],
        model_ref: str,
        label: str,
        max_tool_calls: int | None = None,
    ) -> "Agent":
        endpoint = resolve_agent_endpoint(label, model_ref=model_ref, primary=self._primary_endpoint())
        return Agent(
            model=endpoint.model,
            api_base=endpoint.base_url,
            api_key=endpoint.api_key,
            custom_system_prompt=system_prompt,
            custom_tools=tools,
            is_sub_agent=True,
            max_tool_calls=max_tool_calls,
            permission_mode="plan" if self.permission_mode == "plan" else "bypassPermissions",
            parent_abort_event=self._abort_event,
            workspace=self.workspace,
        )

    def set_confirm_fn(self, fn:Callable[[str], Awaitable[bool]]) -> None:
        self.confirm_fn = fn

    def set_plan_approval_fn(self, fn:Callable[[str], Awaitable[bool]]) -> None:
        self._plan_mode_manager.plan_approval_fn = fn

    def _enter_plan_mode_internal(self) -> None:
        from .observability.trace import trace_event

        if self.permission_mode == "plan":
            return

        self._plan_mode_manager.pre_plan_mode = self.permission_mode
        self.permission_mode = "plan"
        self._plan_mode_manager.plan_dir = self._plan_mode_manager.generate_plan_dir()
        self._system_prompt = self._base_system_prompt + self._plan_mode_manager.build_plan_mode_prompt()
        self.session.system_prompt = self._system_prompt
        print_info("Entered plan mode (read-only). Plan dir: " + str(self._plan_mode_manager.plan_dir))

        trace_event(
            "plan_mode.enter",
            metadata={
                "plan_dir": str(self._plan_mode_manager.plan_dir or ""),
                "previous_mode": self._plan_mode_manager.pre_plan_mode or "",
            },
        )
        self._emit_permission_mode_event()

    def _exit_plan_mode_internal(self, emit: bool = True) -> None:
        if self.permission_mode != "plan":
            return

        self.permission_mode = self._plan_mode_manager.pre_plan_mode or "default"
        self._plan_mode_manager.pre_plan_mode = None
        self._plan_mode_manager.plan_dir = None
        self._system_prompt = self._base_system_prompt
        self.session.system_prompt = self._system_prompt
        print_info(f"Exited plan mode -> {self.permission_mode} mode")
        if emit:
            self._emit_permission_mode_event()

    def _emit_permission_mode_event(self) -> None:
        self.session.append("permission/mode_changed", {
            "session_id": self.session.id,
            "mode": self.permission_mode,
        })

    def toggle_plan_mode(self) -> str:
        if self.permission_mode == "plan":
            self._exit_plan_mode_internal()
        else:
            self._enter_plan_mode_internal()
        return self.permission_mode

    def get_token_usage(self) -> dict:
        return {"input":self.total_input_tokens, "output":self.total_output_tokens}

    def get_messages(self) -> list[dict]:
        return self.session.get_messages_for_llm()

    def set_permission_mode(self, mode: str) -> None:
        if mode == self.permission_mode:
            return
        if mode == "plan":
            # 进入 plan 必须走内部入口：重建 system prompt、创建草稿目录
            self._enter_plan_mode_internal()
            return
        if self.permission_mode == "plan":
            # 离开 plan：恢复 prompt、清理草稿目录（不发中间态事件），再落到目标模式
            self._exit_plan_mode_internal(emit=False)
        self.permission_mode = mode
        self._emit_permission_mode_event()

    def steer(self, message: str) -> None:
        if not hasattr(self, '_steer_queue') or self._steer_queue is None:
            self._steer_queue = []
        self._steer_queue.append(message)

    async def save(self) -> None:
        pass

    @property
    def last_response(self) -> str:
        return self._last_assistant_text

    @property
    def aborted(self) -> bool:
        return self._aborted

    async def chat(self, user_message:str)->None:
        from .core.workspace import set_workspace, reset_workspace
        from .observability.trace import trace_context

        trace_tags = ["sub-agent"] if self.is_sub_agent else ["main-agent"]
        if self.permission_mode == "plan":
            trace_tags.append("plan-mode")

        _ws_token = set_workspace(self.workspace)
        try:
            with trace_context(
                session_id=self.session_id if not self.is_sub_agent else None,
                trace_name="agent-turn",
                tags=trace_tags,
            ):
                await self._chat_inner(user_message)
        finally:
            reset_workspace(_ws_token)

    async def _chat_inner(self, user_message:str)->None:
        print(f"[DEBUG] agent.chat: STARTED - self.session_id = {self.session_id}, is_sub_agent = {self.is_sub_agent}")
        if not self._mcp_initialized and not self.is_sub_agent:
            print(f"[DEBUG] agent.chat: initializing MCP")
            self._mcp_initialized = True
            from .observability.trace import trace_span
            with trace_span("mcp.init") as span:
                try:
                    await asyncio.wait_for(
                        self._mcp_manager.load_and_connect(),
                        timeout=30.0
                    )
                    mcp_defs = self._mcp_manager.get_tool_definitions()
                    if mcp_defs:
                        from agents.tools.mcp_registry import global_registry
                        for mcp_def in mcp_defs:
                            parts = mcp_def["name"].split("__")
                            if len(parts) >= 3:
                                server_name = parts[1]
                                global_registry.register_mcp(mcp_def, server=server_name)
                        self.tools = self.tools + mcp_defs
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
            print(f"[DEBUG] agent.chat: MCP init done")

        print(f"[DEBUG] agent.chat: before cross_session_memory")
        from agents.config import load_config
        _app_cfg = load_config()
        if not self.is_sub_agent and _app_cfg.cross_session_memory:
            from agents.core.session_memory import search_folded_memories, format_folded_memories_for_injection
            related_memories = search_folded_memories(user_message, top_k=3)
            if related_memories:
                memory_context = format_folded_memories_for_injection(related_memories)
                self._system_prompt += memory_context
                self.session.system_prompt = self._system_prompt

        if not self.is_sub_agent and self._turn_number == 0:
            try:
                pending = list_pending_confirm_entries()
                if pending:
                    names = ", ".join(e.name for e in pending[:5])
                    confirm_msg = f"\n<system-reminder>\n有 {len(pending)} 条待确认的调试经验：{names}。输入 /wiki-confirm 确认或 /wiki-reject 拒绝。\n</system-reminder>"
                    self._system_prompt += confirm_msg
                    self.session.system_prompt = self._system_prompt
            except Exception:
                pass

        original_user_message = _safe_utf8_text(user_message)
        ready_skill_extraction_window: dict[str, Any] | None = None
        self._skill_orchestrator.last_retrieved_skill_reference = None

        if not self.is_sub_agent:
            ready_skill_extraction_window = self._skill_orchestrator.pop_pending_extraction_window(
                original_user_message, self._tool_error_streak
            )

        self._aborted = False
        self._abort_event.clear()
        self._turn_number += 1
        self._turn_output_buffer = []
        self._turn_thinking_buffer = []
        self._turn_event_buffer = []
        self._loop_guard_stop_reason = None
        self._tool_budget_stop_reason = None
        self._user_message_written_this_turn = False

        self._current_turn += 1

        from .observability.trace import trace_span

        _turn_event_start_seq = self.session.seq
        _turn_t0 = time.time()
        _turn_start_input_tokens = self.total_input_tokens
        _turn_start_output_tokens = self.total_output_tokens
        with trace_span(
            "turn",
            input=user_message[:4000],
            metadata={
                "session_id": self.session_id,
                "model": self.model,
                "workspace": str(self.workspace),
                "permission_mode": self.permission_mode,
                "turn_id": f"{self.session_id}:{self._current_turn}",
                "turn_number": self._current_turn,
                "event_range_start_seq": _turn_event_start_seq,
                "is_sub_agent": self.is_sub_agent,
            },
        ) as turn_span:
            self._current_trace_id = turn_span.get_trace_id()
            self.session.append("turn/start", {
                "turn": self._current_turn,
                "trace_id": self._current_trace_id,
            })
            coro = self._chat_openai(user_message)
            self._current_task = asyncio.create_task(coro)
            try:
                await self._current_task
            except asyncio.CancelledError:
                self._aborted = True
                if not self._user_message_written_this_turn:
                    self.session.append("user_message", {"content": original_user_message})
                    self._user_message_written_this_turn = True
                turn_span.add_metadata(aborted=True, event_range_end_seq=self.session.seq)
                self.session.append("turn/end", {
                    "turn": self._current_turn,
                    "reason": "aborted",
                    "sub_agent_id": self._current_sub_agent_id,
                    "trace_id": self._current_trace_id,
                })
                raise
            except Exception as e:
                print_error(f"[ERROR] {type(e).__name__}: {e}")
                turn_span.record_error(e)
                turn_span.add_metadata(event_range_end_seq=self.session.seq)
                self.session.append("error", {"message": str(e), "error_type": type(e).__name__, "sub_agent_id": self._current_sub_agent_id})
                self.session.append("turn/end", {
                    "turn": self._current_turn,
                    "reason": "error",
                    "error": str(e),
                    "sub_agent_id": self._current_sub_agent_id,
                    "trace_id": self._current_trace_id,
                })
                return
            finally:
                self._current_task = None
                self._current_trace_id = None
            assistant_text = "".join(self._turn_output_buffer or []).strip()
            self._turn_output_buffer = None
            self._turn_thinking_buffer = None
            self._turn_event_buffer = None

            if self._loop_guard_stop_reason:
                turn_end_reason = "loop_guard"
            elif self._tool_budget_stop_reason:
                turn_end_reason = "budget_exceeded"
            else:
                turn_end_reason = "completed"
            turn_end_event = {
                "turn": self._current_turn,
                "reason": turn_end_reason,
                "sub_agent_id": self._current_sub_agent_id,
                "trace_id": self._current_trace_id,
            }
            if self._loop_guard_stop_reason:
                turn_end_event["loop_guard_reason"] = self._loop_guard_stop_reason
            if self._tool_budget_stop_reason:
                turn_end_event["tool_budget_reason"] = self._tool_budget_stop_reason
                turn_end_event["tool_call_count"] = self._tool_call_count
                turn_end_event["max_tool_calls"] = self.max_tool_calls
            self.session.append("turn/end", turn_end_event)
            turn_span.update(
                output=assistant_text[:20000],
                metadata={
                    "event_range_end_seq": self.session.seq,
                    "aborted": self._aborted,
                    "loop_guard_reason": self._loop_guard_stop_reason,
                    "tool_budget_reason": self._tool_budget_stop_reason,
                    "tool_call_count": self._tool_call_count,
                    "failed_tool_call_count": self._failed_tool_call_count,
                    "max_tool_calls": self.max_tool_calls,
                    "input_tokens_delta": self.total_input_tokens - _turn_start_input_tokens,
                    "output_tokens_delta": self.total_output_tokens - _turn_start_output_tokens,
                    "duration_s": round(time.time() - _turn_t0, 2),
                },
            )
        self._last_assistant_text = assistant_text
        if not self.is_sub_agent and not self._aborted:
            self._skill_orchestrator.turns_since_last_evolution += 1
            if ready_skill_extraction_window and self._skill_orchestrator.should_trigger_evolution():
                self._skill_orchestrator.schedule_background_task(
                    self._skill_orchestrator.run_online_skill_evolution(ready_skill_extraction_window),
                    plan_mode=(self.permission_mode == "plan"),
                )
                self._skill_orchestrator.record_evolution_event()

            self._skill_orchestrator.set_pending_extraction_window(
                messages=self._skill_orchestrator.get_recent_dialog_messages(self.session.get_messages_for_llm(), max_messages=8),
                original_user_message=original_user_message,
                assistant_text=assistant_text,
                retrieved_reference=self._skill_orchestrator.last_retrieved_skill_reference,
                tool_error_streak=self._tool_error_streak,
            )
        if not self.is_sub_agent:
            try:
                print_divider()
            except Exception:
                pass

    async def run_once(self, prompt: str) -> dict:
        self._output_buffer = []
        prev_in = self.total_input_tokens
        prev_out = self.total_output_tokens
        await self.chat(prompt)
        text = "".join(self._output_buffer)
        self._output_buffer = None
        stop_reason = self._loop_guard_stop_reason or self._tool_budget_stop_reason
        return {
            "text": text,
            "tokens": {
                "input": self.total_input_tokens - prev_in,
                "output": self.total_output_tokens - prev_out
            },
            "stop_reason": stop_reason,
            "tool_budget_exceeded": bool(self._tool_budget_stop_reason),
            "tool_call_count": self._tool_call_count,
            "failed_tool_call_count": self._failed_tool_call_count,
        }

    def _emit_text(self, text: str) -> None:
        text = _safe_utf8_text(text)
        if self._turn_output_buffer is not None:
            self._turn_output_buffer.append(text)
        if self._output_buffer is not None:
            self._output_buffer.append(text)
        else:
            print_assistant_text(text)

        event_data = {"content": text, "turn": self._current_turn, "step": self._current_step}
        if self._current_sub_agent_id:
            event_data["sub_agent_id"] = self._current_sub_agent_id
        self.session.append("text", event_data)

    def _build_fold_guidance_section(self) -> str:
        if self._custom_system_prompt is not None:
            return ""
        utilization = self.estimated_context_tokens / self.effective_window if self.effective_window else 0.0
        last_fold = "never" if not self._fold_last_time else f"{int((time.time() - self._fold_last_time) / 60)}m ago"
        return (
            "\n\n# Runtime Fold Guidance\n"
            f"- Current context utilization: {utilization:.0%}\n"
            f"- Recent tool error streak: {self._tool_error_streak}\n"
            f"- Same tool repeat count: {self._same_tool_repeat_count}\n"
            f"- Last fold: {last_fold}\n"
            "- If the context is getting long, the same tool is being retried without progress, or tool failures are accumulating, call `compact_context` before trying more tools.\n"
            "- If you folded very recently and the next step is clear, prefer continuing rather than folding again.\n"
        )

    def _refresh_runtime_system_prompt(self) -> None:
        if self._custom_system_prompt is not None:
            self.session.system_prompt = self._custom_system_prompt
            return
        from .core.workspace import set_workspace, reset_workspace
        _ws_token = set_workspace(self.workspace)
        try:
            self._base_system_prompt = build_system_prompt()
            if self.permission_mode == "plan":
                self._system_prompt = self._base_system_prompt + self._plan_mode_manager.build_plan_mode_prompt()
            else:
                self._system_prompt = self._base_system_prompt
            self._system_prompt += self._build_fold_guidance_section()
            self.session.system_prompt = self._system_prompt
        finally:
            reset_workspace(_ws_token)

    def _record_tool_outcome(self, tool_name: str, success: bool) -> None:
        if tool_name == self._last_tool_name:
            self._same_tool_repeat_count += 1
        else:
            self._same_tool_repeat_count = 1
        self._last_tool_name = tool_name
        if success:
            self._tool_error_streak = 0
        else:
            self._tool_error_streak += 1

    def record_tool_outcome(self, tool_name: str, success: bool) -> None:
        """Public alias for _record_tool_outcome (used by agent_loop)."""
        self._record_tool_outcome(tool_name, success)

    @staticmethod
    def _canonicalize_arguments(args: dict) -> str:
        def sort_json(value):
            if isinstance(value, dict):
                return {k: sort_json(v) for k, v in sorted(value.items())}
            if isinstance(value, list):
                return [sort_json(v) for v in value]
            return value
        return json.dumps(sort_json(args), ensure_ascii=False, sort_keys=True)

    def _check_repeat_guard(self, tool_name: str, inp: dict) -> str | None:
        key = json.dumps([tool_name, self._canonicalize_arguments(inp)], ensure_ascii=False)
        if key == self._repeat_chain_key:
            self._repeat_chain_count += 1
        else:
            self._repeat_chain_key = key
            self._repeat_chain_count = 1
        if self._repeat_chain_count == 3:
            return (
                "You are repeating the exact same tool call with identical arguments. "
                "Carefully analyze the previous result before calling again: if the task is "
                "not complete, try a different approach or different arguments instead of "
                "repeating the call."
            )
        if self._repeat_chain_count in (5, 8):
            preview = self._canonicalize_arguments(inp)[:500]
            return (
                f"Repeated tool call detected:\n"
                f"- tool: {tool_name}\n"
                f"- consecutive_calls: {self._repeat_chain_count}\n"
                f"- arguments: {preview}\n"
                f"The repeated calls are not making progress. Do not call this tool with "
                f"these exact arguments again. Inspect the latest result and choose a "
                f"different action, different arguments, or finish the task if enough "
                f"evidence has been gathered."
            )
        return None

    def _record_fold_event(self) -> None:
        self._compressor._record_fold_event()
        self._fold_last_time = self._compressor._fold_last_time
        self._fold_count = self._compressor._fold_count
        self._tool_error_streak = 0
        self._same_tool_repeat_count = 0
        self._last_tool_name = ""
        self._repeat_chain_key = ""
        self._repeat_chain_count = 0

    def _looks_like_tool_failure(self, tool_name: str, raw: str, result: str) -> bool:
        return detect_failure(tool_name, raw, result)

    def clear_history(self)->None:
        self._context_manager.clear_history()

    @property
    def messages(self) -> list[dict]:
        return self.session.get_messages_for_llm()

    @property
    def openai_client(self):
        return self._openai_client

    @property
    def stream_event_queue(self) -> asyncio.Queue | None:
        return None

    @property
    def current_sub_agent_id(self) -> str | None:
        return self._current_sub_agent_id

    @property
    def confirmed_paths(self) -> set[str]:
        return self._confirmed_paths

    @property
    def context_cleared(self) -> bool:
        return self._context_cleared

    def abort_requested(self) -> bool:
        return self._abort_requested()

    def mark_aborted(self) -> None:
        self._aborted = True

    def append_user_message(self, content: str, snapshot_id: str | None = None) -> None:
        self._context_manager.append_user_message(content, snapshot_id)

    def append_memory_injection(self, content: str) -> None:
        self._context_manager.append_memory_injection(content)

    def append_tool_message(self, tool_call_id: str, content: str, tool_name: str = "") -> None:
        self._context_manager.append_tool_message(tool_call_id, content, tool_name)
        # 记录每个工具的结果字符数
        if tool_name:
            self._tool_result_chars[tool_name] = self._tool_result_chars.get(tool_name, 0) + len(content)

    def reset_repeat_chain(self) -> None:
        self._repeat_chain_key = ""
        self._repeat_chain_count = 0

    def build_side_query(self, *, max_tokens: int = 2000):
        return self._build_side_query(max_tokens=max_tokens)

    def start_wiki_prefetch(self, user_message: str, side_query) -> None:
        if self.is_sub_agent or self._wiki_prefetch is not None:
            return
        cooled_wiki_paths = {
            path for path, turn in self._wiki_surfaced_at.items()
            if self._turn_number - turn < self.WIKI_RECALL_COOLDOWN_TURNS
        }
        self._wiki_prefetch_consumed = False
        self._wiki_prefetch = asyncio.create_task(
            select_relevant_wiki_entries(user_message, side_query, cooled_wiki_paths)
        )

    def add_input_tokens(self, count: int) -> None:
        self.total_input_tokens += count

    def add_output_tokens(self, count: int) -> None:
        self.total_output_tokens += count

    def set_last_usage_tokens(self, input_count: int, total_count: int) -> None:
        self.last_input_token_count = max(0, int(input_count))
        self.last_total_token_count = max(0, int(total_count))

    def mark_last_usage_position(self, seq: int) -> None:
        self.last_usage_seq = int(seq)

    def reset_context_token_estimate(self) -> None:
        self.last_input_token_count = 0
        self.last_total_token_count = 0
        self.last_usage_seq = -1

    @property
    def estimated_context_tokens(self) -> int:
        if self.last_total_token_count <= 0:
            return 0
        from .core.context_events import estimate_tokens_after_seq
        return self.last_total_token_count + estimate_tokens_after_seq(self.session, self.last_usage_seq)

    def increment_turns(self) -> None:
        self.current_turns += 1

    def check_budget(self) -> dict:
        return self._check_budget()

    def publish_stream_event(self, event: dict) -> None:
        event_type = event.pop("type", "info")
        self.session.append(event_type, event)

    def publish_tool_call_event(self, call_id: str, name: str, inp: dict) -> None:
        event_data = {"call_id": call_id, "name": name, "input": inp, "turn": self._current_turn, "step": self._current_step}
        if self._current_sub_agent_id:
            event_data["sub_agent_id"] = self._current_sub_agent_id
        self.session.append("tool_call", event_data)

    def publish_tool_result_event(
        self,
        call_id: str,
        name: str,
        result: str,
        status: str,
        snapshot: dict | None = None,
        outcome: str | None = None,
        metadata: dict | None = None,
    ) -> None:
        self._tool_call_count += 1
        if status != "ok" or (outcome and outcome != "success"):
            self._failed_tool_call_count += 1

        event_data = {"call_id": call_id, "name": name, "result": result, "status": status, "turn": self._current_turn, "step": self._current_step}
        if outcome:
            event_data["outcome"] = outcome
        if metadata:
            event_data["metadata"] = metadata
        if self._current_sub_agent_id:
            event_data["sub_agent_id"] = self._current_sub_agent_id
        if snapshot:
            event_data["snapshot"] = snapshot
        self.session.append("tool_result", event_data)

    def record_tools_outcome(self, tool_name: str, success: bool) -> None:
        self._record_tool_outcome(tool_name, success)

    def set_current_tool_name(self, name: str | None) -> None:
        self._current_tool_name = name

    def add_confirmed_path(self, path: str) -> None:
        self._confirmed_paths.add(path)

    def execute_tool_call(self, name: str, inp: dict):
        return self._tool_dispatcher.execute_tool_call(name, inp)

    def persist_large_result(self, tool_name: str, result: str) -> str:
        return self._persist_large_result(tool_name, result)

    def looks_like_tool_failure(self, tool_name: str, raw: str, result: str) -> bool:
        return self._looks_like_tool_failure(tool_name, raw, result)

    def check_repeat_guard(self, tool_name: str, inp: dict) -> str | None:
        return self._check_repeat_guard(tool_name, inp)

    def clear_context_flag(self) -> None:
        self._context_cleared = False

    def refresh_runtime_system_prompt(self) -> None:
        self._refresh_runtime_system_prompt()

    def check_and_compact(self):
        return self._context_manager._check_and_compact()

    def emit_text(self, text: str) -> None:
        self._emit_text(text)

    def append_thinking_text(self, text: str) -> None:
        if self._turn_thinking_buffer is not None:
            self._turn_thinking_buffer.append(text)

    def get_thinking_content(self) -> str | None:
        return "".join(self._turn_thinking_buffer or []).strip() if self._turn_thinking_buffer else None

    async def confirm_dangerous(self, message: str) -> bool:
        return await self._confirm_dangerous(message)

    async def drain_background_skill_tasks(self) -> None:
        await self._skill_orchestrator.drain_background_tasks()

    async def extract_now(self, hint: str = "") -> dict[str, Any]:
        return await self._skill_orchestrator.extract_now(hint, confirm_fn=self.confirm_fn)

    def show_cost(self):
        total = self._get_current_cost_usd()
        budget_info = f" / ${self.max_cost_usd} budget" if self.max_cost_usd else ""
        turn_info = f" | Turns: {self.current_turns}/{self.max_turns}" if self.max_turns else ""
        print_info(
            f"Tokens: {self.total_input_tokens} in / {self.total_output_tokens} out\n  Estimated cost: ${total:.4f}{budget_info}{turn_info}")

    def status_line(self) -> str:
        estimated_tokens = self.estimated_context_tokens
        if estimated_tokens > 0:
            util = estimated_tokens / self.effective_window if self.effective_window else 0.0
            ctx_part = f"ctx: {estimated_tokens}/{self.effective_window} effective tokens ({util:.0%})"
        else:
            ctx_part = f"ctx: -/{self.effective_window} effective tokens (未知，待首次调用)"
        return (
            f"model: {self.model} | {ctx_part} | session: {self.total_input_tokens} in / {self.total_output_tokens} out"
        )

    def _get_current_cost_usd(self) -> float:
        return (self.total_input_tokens / 1_000_000) * 3 + (self.total_output_tokens / 1_000_000) * 15

    def tool_budget_exceeded(self) -> bool:
        return self.max_tool_calls is not None and self._tool_call_count >= self.max_tool_calls

    def _check_budget(self) -> dict:
        if self.max_cost_usd is not None and self._get_current_cost_usd() >= self.max_cost_usd:
            return {"exceeded": True, "kind": "cost", "reason": f"Cost limit reached (${self._get_current_cost_usd():.4f} >= ${self.max_cost_usd})"}
        if self.max_turns is not None and self.current_turns >= self.max_turns:
            return {"exceeded": True, "kind": "turns", "reason": f"Turn limit reached ({self.current_turns} >= {self.max_turns})"}
        if self.tool_budget_exceeded():
            return {
                "exceeded": True,
                "kind": "tool_calls",
                "reason": f"Tool call limit reached ({self._tool_call_count} >= {self.max_tool_calls})",
            }
        return {"exceeded": False}

    async def compact(self)->None:
        await self._context_manager.compact()

    def restore_session(self, data: dict) -> None:
        from agents.core.session_lifecycle import SessionState
        from agents.logging import print_info

        state = SessionState(session_id=self.session_id, model=self.model)
        self._session_lifecycle.restore(state, data, self.session)
        print_info(f"Session restored ({self._get_message_count()} messages).")

    async def rewind_turns(self, n: int = 1) -> str:
        """统一回退：对话回退 N 轮 + 文件恢复到快照（原子操作）。"""
        from agents.core.rewind_service import get_rewind_service
        svc = get_rewind_service()
        plan = await svc.stage(
            self.session_id, turns=n, session=self.session, workspace=str(self.workspace)
        )
        result = await svc.commit(plan.id, session=self.session)
        msg = (
            f"Rewound {n} turn(s): removed {result['removed_user_messages']} user messages, "
            f"{result['removed_events']} events"
        )
        if result["restored_files"]:
            msg += f", restored {len(result['restored_files'])} files"
        return msg

    def fork_session(self) -> str:
        from agents.core.session_lifecycle import SessionState

        state = SessionState(
            session_id=self.session_id,
            model=self.model,
            start_time=self.session_start_time,
            cwd=str(self.workspace),
        )
        result, new_session = self._session_lifecycle.fork(state, self.session)
        self.session = new_session
        self.session_id = new_session.id
        return result

    def describe_context(self) -> list[dict]:
        return self._context_manager.describe_context()

    def delete_context_messages(self, indexes: list[int]) -> str:
        return self._context_manager.delete_context_messages(indexes)

    def keep_context_messages(self, indexes: list[int]) -> str:
        return self._context_manager.keep_context_messages(indexes)

    def _get_message_count(self) -> int:
        return self._context_manager._get_message_count()

    WIKI_RECALL_COOLDOWN_TURNS = 5

    async def _check_and_compact(self)->None:
        await self._context_manager._check_and_compact()

    async def _compact_conversation(self, *, trigger: str = "manual")->bool:
        return await self._context_manager._compact_conversation(trigger=trigger)

    async def _compact_openai(self, *, trigger: str)->bool:
        return await self._context_manager._compact_openai(trigger=trigger)

    def _persist_large_result(self, tool_name: str, result: str) -> str:
        return persist_large_result(tool_name, result)

    def _format_plan_tasks_block(self, exec_result: dict) -> str:
        """把 start_plan_execution 结果格式化为注入对话的任务清单+执行指令（含策略插件）。"""
        msg = "\n\n## Plan Tasks Ready\n"
        msg += f"Status: {exec_result.get('status', 'unknown')}\n"
        msg += f"Total tasks: {exec_result.get('total_tasks', 0)}\n"
        msg += f"Pending tasks: {exec_result.get('pending_tasks', 0)}\n\n"

        tasks = exec_result.get("tasks", [])
        if not tasks:
            return msg + "No pending tasks found."

        msg += "## Task List\n\n"
        for task in tasks:
            msg += f"### Task {task['id']}: {task['title']}\n"
            msg += f"- **File**: `{task.get('file', 'N/A')}`\n"
            msg += f"- **Acceptance**: {task.get('acceptance', 'N/A')}\n"
            msg += f"- **Status**: {task['status']}\n\n"

        msg += "\n## Instructions\n\n"
        msg += "Please execute these tasks one by one. For each task:\n"
        msg += "1. Call `plan_task_start(slug, task_id)` before starting\n"
        msg += "2. Implement the task (write code, create files, etc.)\n"
        msg += "3. Verify the implementation (run the acceptance command, check output)\n"
        msg += "4. Call `plan_task_done(slug, task_id, commit, verification)` after success — verification is REQUIRED: a JSON object with the verify `command` and its `exit_code`\n"
        msg += "5. If failed, call `plan_task_failed(slug, task_id, error)`\n"
        msg += "6. After all tasks are done, call `plan_complete(slug)`\n"

        slug = exec_result.get("slug", "")
        if slug:
            try:
                from agents.plan.plan_executor import PlanExecutor
                executor = PlanExecutor.for_plan(slug)
                execute_guide = executor.build_execute_instructions()
                if execute_guide:
                    msg += f"\n## Execution Strategy: {executor.strategy_config.get('execute', 'direct')}\n\n{execute_guide}\n"
                converge_guide = executor.build_converge_guidance()
                if converge_guide:
                    msg += f"\n## Pre-Completion Converge Check（调用 plan_complete 前必须完成）\n\n{converge_guide}\n"
            except Exception as e:
                print(f"[WARN] plan strategy injection failed: {e!r}")

        return msg

    async def _execute_plan_mode_tool(self, name):
        from .observability.trace import trace_event

        if name == "enter_plan_mode":
            if self.permission_mode == "plan":
                return "Already in plan mode."
            self._enter_plan_mode_internal()
            return (
                f"Entered plan mode. You are now in read-only mode.\n\n"
                f"Your plan directory: {self._plan_mode_manager.plan_dir}\n"
                f"双轨规划（二选一）：\n"
                f"- 轻量轨（默认）：只写 plan.md（## 背景 / ## 方案 / ## 任务清单（checkbox：- [ ] 1. 描述）/ ## 验收）\n"
                f"- 重量轨（复杂任务）：写 spec.md（含验收标准）+ design.md + tasks.md\n\n"
                f"When your plan is complete, call exit_plan_mode."
            )

        if name == "exit_plan_mode":
            if self.permission_mode != "plan":
                return "Not in plan mode."
            
            # 校验产物完整性
            validation = self._plan_mode_manager.validate_plan_artifacts()
            if not validation["valid"]:
                errors = "\n".join(f"- {e}" for e in validation["errors"])
                return f"Plan artifacts validation failed:\n{errors}\n\nPlease complete your plan before exiting."
            
            # 读取产物内容（自动判定轻量轨/重量轨）
            draft = self._plan_mode_manager.read_draft_artifacts()
            granularity = validation.get("granularity", draft["granularity"])
            spec_content = draft["spec"]
            plan_content = draft["design"]
            tasks_content = draft["tasks"]
            plan_md = draft["plan"]
            if granularity == "minimal" and not tasks_content:
                from agents.plan.plan_mode import PlanModeManager
                tasks_content = PlanModeManager.checkbox_tasks_to_structured(plan_md)

            full_plan = "\n\n".join(
                part for part in (
                    plan_md.strip() if granularity == "minimal" and plan_md.strip() else "",
                    f"## Spec\n{spec_content}" if spec_content.strip() else "",
                    f"## Design\n{plan_content}" if plan_content.strip() else "",
                    f"## Tasks\n{tasks_content}" if granularity != "minimal" and tasks_content.strip() else "",
                ) if part
            ) or "(empty plan)"

            if self._plan_mode_manager.plan_approval_fn:
                result = await self._plan_mode_manager.plan_approval_fn(full_plan)
                choice = result.get("choice", "manual-execute")

                if choice == "keep-planning":
                    feedback = result.get("feedback") or "Please revise the plan."

                    trace_event(
                        "plan_mode.rejected",
                        input=feedback[:2000] if feedback else "",
                        metadata={"feedback": feedback[:2000] if feedback else ""},
                    )

                    return (
                        f"User rejected the plan and wants to keep planning.\n\n"
                        f"User feedback: {feedback}\n\n"
                        f"Please revise your plan based on this feedback. When done, call exit_plan_mode again."
                    )

                if choice in ("clear-and-execute", "execute"):
                    plan_result = self._plan_mode_manager.handle_plan_system_integration(
                        spec_content, plan_content, tasks_content, self.session,
                        granularity=granularity, plan_md=plan_md,
                    )
                    target_mode = "acceptEdits"
                else:
                    target_mode = self._plan_mode_manager.pre_plan_mode or "default"
                    plan_result = None

                saved_plan_dir = self._plan_mode_manager.plan_dir
                self.permission_mode = target_mode
                self._plan_mode_manager.pre_plan_mode = None
                self._plan_mode_manager.plan_dir = None
                self._system_prompt = self._base_system_prompt
                self.session.system_prompt = self._system_prompt
                self._emit_permission_mode_event()

                trace_event(
                    "plan_mode.approved",
                    metadata={
                        "target_mode": target_mode,
                        "context_cleared": choice == "clear-and-execute",
                        "plan_slug": plan_result.get("slug", "") if plan_result else "",
                    },
                )

                if choice == "clear-and-execute":
                    self._context_manager.clear_history_keep_system()
                    self._context_cleared = True
                    print_info(f"Plan approved. Context cleared, executing in {target_mode} mode.")

                    result_msg = f"User approved the plan. Context was cleared. Permission mode: {target_mode}\n\n"
                    if plan_result:
                        result_msg += f"Plan system entry created: {plan_result.get('slug', '')}\n\n"
                    result_msg += f"Plan directory: {saved_plan_dir}\n\n"
                    result_msg += f"## Approved Plan:\n{full_plan}\n\n"

                    if plan_result and plan_result.get("slug"):
                        from agents.plan.plan_manager import start_plan_execution
                        plan_slug = plan_result["slug"]
                        print_info(f"Starting plan execution: {plan_slug}")

                        exec_result = start_plan_execution(plan_slug)
                        result_msg += self._format_plan_tasks_block(exec_result)
                    else:
                        result_msg += "Proceed with implementation."

                    return result_msg

                print_info(f"Plan approved. Executing in {target_mode} mode.")
                result_msg = (
                    f"User approved the plan. Permission mode: {target_mode}\n\n"
                    f"## Approved Plan:\n{full_plan}\n\n"
                )

                if plan_result and plan_result.get("slug"):
                    from agents.plan.plan_manager import start_plan_execution
                    plan_slug = plan_result["slug"]
                    print_info(f"Starting plan execution: {plan_slug}")

                    exec_result = start_plan_execution(plan_slug)
                    result_msg += self._format_plan_tasks_block(exec_result)
                else:
                    result_msg += "Proceed with implementation."

                return result_msg

            confirmed = await self._confirm_dangerous(
                f"Plan completed. Exit plan mode?\n\n{full_plan}",
                extra_data={"plan_dir": str(self._plan_mode_manager.plan_dir)} if self._plan_mode_manager.plan_dir else None,
                tool_name="exit_plan_mode",
            )

            if not confirmed:
                feedback = self._permission_gate.last_feedback or "User rejected the plan without comments."
                trace_event(
                    "plan_mode.rejected",
                    input=feedback[:2000],
                    metadata={"feedback": feedback[:2000]},
                )

                return (
                    "User rejected the plan and wants to keep planning.\n\n"
                    f"User feedback: {feedback}\n\n"
                    "Please revise your plan based on this feedback. When done, call exit_plan_mode again."
                )

            choice = self._permission_gate.last_choice or "execute"
            plan_result = self._plan_mode_manager.handle_plan_system_integration(
                spec_content, plan_content, tasks_content, self.session,
                granularity=granularity, plan_md=plan_md,
            )

            saved_plan_dir = self._plan_mode_manager.plan_dir
            if choice == "manual-execute":
                target_mode = self._plan_mode_manager.pre_plan_mode or "default"
            else:  # execute / clear-and-execute → 自动执行
                target_mode = "acceptEdits"
            self.permission_mode = target_mode
            self._plan_mode_manager.pre_plan_mode = None
            self._plan_mode_manager.plan_dir = None
            self._system_prompt = self._base_system_prompt
            self.session.system_prompt = self._system_prompt
            self._emit_permission_mode_event()

            trace_event(
                "plan_mode.approved",
                metadata={
                    "target_mode": target_mode,
                    "choice": choice,
                    "context_cleared": choice == "clear-and-execute",
                    "plan_slug": plan_result.get("slug", "") if plan_result else "",
                },
            )

            if choice == "clear-and-execute":
                self._context_manager.clear_history_keep_system()
                self._context_cleared = True
                print_info(f"Plan approved. Context cleared, executing in {target_mode} mode.")
                result_msg = f"User approved the plan. Context was cleared. Permission mode: {target_mode}\n\n"
                if plan_result:
                    result_msg += f"Plan system entry created: {plan_result.get('slug', '')}\n\n"
                result_msg += f"Plan directory: {saved_plan_dir}\n\n"
            else:
                print_info(f"Plan approved. Executing in {target_mode} mode.")
                result_msg = f"User approved the plan. Permission mode: {target_mode}\n\n"
                if plan_result:
                    result_msg += f"Plan system entry created: {plan_result.get('slug', '')}\n\n"
            result_msg += f"## Approved Plan:\n{full_plan}\n\n"

            if plan_result and plan_result.get("slug"):
                from agents.plan.plan_manager import start_plan_execution
                plan_slug = plan_result["slug"]
                print_info(f"Starting plan execution: {plan_slug}")

                exec_result = start_plan_execution(plan_slug)
                result_msg += self._format_plan_tasks_block(exec_result)
            else:
                result_msg += "Proceed with implementation."

            return result_msg

        return f"Unknown plan mode tool: {name}"

    def _clear_history_keep_system(self) -> None:
        self._context_manager.clear_history_keep_system()

    async def _chat_openai(self, user_message: str) -> None:
        await self._loop.run(user_message)

    async def _call_openai_stream(self) -> dict:
        return await self._loop.call_model_stream()

    async def _confirm_dangerous(self, command: str, extra_data: dict | None = None, tool_name: str | None = None) -> bool:
        self._permission_gate.set_session(self.session)
        self._permission_gate.set_confirm_fn(self.confirm_fn) if self.confirm_fn else None
        self._permission_gate.set_sub_agent_id(self._current_sub_agent_id)
        self._permission_gate.set_abort_fn(self._abort_requested)
        self._permission_gate.set_current_tool_name(tool_name or getattr(self, '_current_tool_name', 'unknown'))
        return await self._permission_gate.confirm(command, extra_data=extra_data)

    async def request_permission(self, request_id: str, command: str, tool_name: str,
                                  message: str = "", sub_agent_id: str | None = None,
                                  timeout: float = 300.0) -> bool:
        rpc_id = str(uuid.uuid4())
        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._permission_waitters[rpc_id] = future

        self.session.append("permission/request", {
            "rpc_id": rpc_id,
            "request_id": request_id,
            "command": command,
            "tool_name": tool_name,
            "message": message,
            "sub_agent_id": sub_agent_id,
        })

        try:
            result = await asyncio.wait_for(future, timeout=timeout)
            allowed = result["allowed"]
            self.session.append("permission/resolved", {
                "rpc_id": rpc_id,
                "outcome": "allowed" if allowed else "denied",
            })
            return allowed
        except asyncio.TimeoutError:
            self._permission_waiters.pop(rpc_id, None)
            return False

    def respond_permission(self, rpc_id: str, allowed: bool) -> bool:
        future = self._permission_waiters.pop(rpc_id, None)
        if not future:
            return False
        future.set_result({"allowed": allowed})
        return True

    def set_permission_response(self, request_id: str, allowed: bool, feedback: str = "", choice: str = "") -> None:
        self._permission_gate.set_response(request_id, allowed, feedback, choice)
