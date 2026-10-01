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
from agents.observability.tool_tracker import RepeatGuard
from agents.wiki.wiki_manager import start_wiki_prefetch as run_wiki_prefetch
from agents.core.model_registry import ModelEndpoint, resolve_agent_endpoint
from agents.core import prompt_runtime
from agents.core.side_query import SideQueryFactory
from agents.core import session_lifecycle
from agents.core.subagent_runner import spawn_sub_agent
from agents.core.steer_queue import MessageQueue
from agents.core.text_sanitization import safe_utf8_text
from agents.core.turn_runner import TurnRunner
from agents.core.session import Session
from agents.tools import ToolDef, tool_definitions
# 模块级而非延迟 import：tests/unit/test_import_hygiene.py 冻结了「agents/ 内零
# 函数级 agents import」（M4 依赖治理，豁免表为空），其自身指引是解环而不是延迟。
# 依赖方向早已存在（上一行 from agents.tools import ...，agents/core/prompt_runtime.py
# 也模块级读 task_store）；task_store 只依赖 workspace 与 logging，不构成环。
# 刻意绑模块而不是 `from ... import list_tasks`：折叠门要能被测试替换掉
# list_tasks 来验证「读不出来就照常折叠」这条失败方向。
from agents.tools import task_store
from agents.logging import print_info, print_assistant_text, print_error
from agents.plan.plan_mode import PlanModeManager
from agents.tools.dispatcher import ToolDispatcher
from agents.core.context import ContextManager
from agents.config import DEFAULT_AUTO_COMPACT_THRESHOLD, DEFAULT_CONTEXT_WINDOW, get_endpoint_by_model
from agents.core.auto_wake import auto_wake_budget_exhausted
from agents.core.context_compressor import COMPRESSION_ARMS
from agents.core.options import AgentOptions


def _get_max_output_tokens(model: str) -> int:
    m = model.lower()
    if "opus-4-6" in m:
        return 64000
    if "sonnet-4-6" in m:
        return 32000
    if any(x in m for x in ("opus-4", "sonnet-4", "haiku-4")):
        return 32000
    return 16384


class Agent:
    def __init__(self,
                 *,
                 permission_mode:str="default",
                 model:str="deepseek-chat",
                 api_base: str | None=None,
                 api_key: str | None=None,
                 thinking: bool | None = None,
                 thinking_feedback: bool | None = None,
                 compression_arm: str | None = None,
                 context_window: int | None = None,
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
        # 段1：参数归一——显式 kwargs 收敛为 AgentOptions（传入 options 实例则整体覆盖，
        # 与原"options 展开"语义一致；非 AgentOptions 的 options 值按原语义忽略）
        if not isinstance(options, AgentOptions):
            options = AgentOptions(
                permission_mode=permission_mode, model=model, api_base=api_base, api_key=api_key,
                thinking=thinking, thinking_feedback=thinking_feedback,
                compression_arm=compression_arm, context_window=context_window,
                max_cost_usd=max_cost_usd, max_turns=max_turns, max_tool_calls=max_tool_calls,
                confirm_fn=confirm_fn, custom_system_prompt=custom_system_prompt,
                custom_tools=custom_tools, is_sub_agent=is_sub_agent,
                parent_abort_event=parent_abort_event, workspace=workspace,
                session_id=session_id,
            )
        self._init_config(options)
        self._init_state(options)
        self._init_runtime_state(options)
        self._init_prompt(options)
        self._loop = AgentLoop(self)
        self._turn_runner = TurnRunner(self)
        self._tool_dispatcher = ToolDispatcher(agent_ref=self)
        self._context_manager = ContextManager(agent_ref=self)

    def _init_config(self, opts: "AgentOptions") -> None:
        """段2：基础配置 + 端点解析（thinking/压缩臂/上下文窗口）+ 会话身份。"""
        self.permission_mode = opts.permission_mode
        self.thinking = opts.thinking
        self.thinking_feedback = opts.thinking_feedback
        self.model = opts.model
        self.is_sub_agent = opts.is_sub_agent
        self.tools = opts.custom_tools if opts.custom_tools is not None else tool_definitions
        self.max_cost_usd = opts.max_cost_usd
        self.max_turns = opts.max_turns
        self.max_tool_calls = opts.max_tool_calls
        self.confirm_fn = opts.confirm_fn
        self._custom_system_prompt = opts.custom_system_prompt
        self.workspace: Path = Path(opts.workspace).resolve() if opts.workspace else Path.cwd()
        self._api_base = opts.api_base
        self._api_key = opts.api_key
        self._side_query = SideQueryFactory(self)
        _ep = get_endpoint_by_model(opts.model)
        self.auto_compact_threshold = DEFAULT_AUTO_COMPACT_THRESHOLD
        # thinking 解析链：请求级覆盖 > 端点配置 > None（跟随模型默认）
        if self.thinking is None and _ep is not None:
            self.thinking = _ep.thinking
        # thinking_feedback 解析链：请求级覆盖 > 端点配置 > False（剥离历史思考）
        if self.thinking_feedback is None:
            self.thinking_feedback = bool(_ep.thinking_feedback) if _ep is not None else False
        self.compression_arm = opts.compression_arm or "full"
        if self.compression_arm not in COMPRESSION_ARMS:
            raise ValueError(f"compression_arm must be one of {sorted(COMPRESSION_ARMS)}, got {self.compression_arm!r}")
        # 窗口解析优先级：显式覆盖（评测消融）> 端点配置 > 默认
        if opts.context_window is not None:
            self.context_window = opts.context_window
        else:
            self.context_window = _ep.context_window if _ep else DEFAULT_CONTEXT_WINDOW
        self.effective_window = self.context_window - 20000
        self.session_id = opts.session_id or uuid.uuid4().hex[:8]
        self.session_start_time = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    def _init_state(self, opts: "AgentOptions") -> None:
        """段3：会话 + token/turn 计数 + 核心组件（compressor/permission/lifecycle/skill）。"""
        self.session = Session(self.session_id, origin="sub_agent" if opts.is_sub_agent else None)
        self.session.thinking_feedback = self.thinking_feedback
        self._current_turn: int = 0
        self._current_step: int = 0
        self._user_message_written_this_turn: bool = False
        # D3 hotfix：待注入的记忆/提醒（chat() 计算，_prepare_turn 落盘为 memory_injection 事件）
        self._pending_system_injections: list[str] = []
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
            arm=self.compression_arm,
        )
        self._permission_gate = PermissionGate()
        self._session_lifecycle = SessionLifecycle()
        self._skill_orchestrator = SkillOrchestrator(
            side_query_fn=self._build_side_query,
            permission_mode=self.permission_mode,
            session_id=self.session_id,
            refresh_system_prompt=self._refresh_runtime_system_prompt,
        )

    def _init_runtime_state(self, opts: "AgentOptions") -> None:
        """段4：运行时纯状态（abort/缓冲/MCP/wiki 预取/steer 队列/守卫计数）。"""
        self._aborted = False
        self._abort_event = asyncio.Event()
        self._parent_abort_event = opts.parent_abort_event
        self._current_task: asyncio.Task | None = None
        # U3b：turn 级互斥（用户轮/自动唤醒轮单一写者）+ 唤醒任务句柄
        self._turn_lock = asyncio.Lock()
        self._auto_wake_task: asyncio.Task | None = None
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

        self._fold_last_time: float = 0.0
        self._fold_count: int = 0
        # U1：steering/follow_up 双队列（steer() 写入，agent_loop step 边界 drain）
        self.message_queue = MessageQueue()
        self._tool_error_streak: int = 0
        self._same_tool_repeat_count: int = 0
        self._last_tool_name: str = ""
        self._repeat_guard = RepeatGuard()
        self._loop_guard_stop_reason: str | None = None
        self._tool_budget_stop_reason: str | None = None
        self._tool_result_chars: dict[str, int] = {}  # 每个工具的结果字符数统计
        self._tool_call_count: int = 0
        self._failed_tool_call_count: int = 0

    def _init_prompt(self, opts: "AgentOptions") -> None:
        """段5：system prompt（含 plan 模式）+ OpenAI client（workspace 上下文内构建）。

        U5a：渲染统一走 _refresh_runtime_system_prompt（旧实现在此处直接
        build_system_prompt 后 refresh 再渲染一次——初始化双重渲染）。
        """
        from .core.workspace import set_workspace, reset_workspace
        _ws_token = set_workspace(self.workspace)
        try:
            self._system_prompt_breakdown: dict[str, int] = {}
            self._plan_mode_manager = PlanModeManager(
                workspace=self.workspace,
                session_id=self.session_id,
                plan_approval_fn=None,
            )

            if self.permission_mode == "plan":
                self._plan_mode_manager.plan_dir = self._plan_mode_manager.generate_plan_dir()

            self._openai_client = openai.AsyncOpenAI(base_url=opts.api_base, api_key=opts.api_key)

            self._refresh_runtime_system_prompt()
        finally:
            reset_workspace(_ws_token)

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
        return self._side_query.get_client()

    def _build_side_query(self, *, max_tokens: int = 256):
        return self._side_query.build(max_tokens=max_tokens)

    def abort(self) -> None:
        self._aborted = True
        self._abort_event.set()
        if self._auto_wake_task and not self._auto_wake_task.done():
            self._auto_wake_task.cancel()
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
        return spawn_sub_agent(
            self,
            system_prompt=system_prompt,
            tools=tools,
            model_ref=model_ref,
            label=label,
            max_tool_calls=max_tool_calls,
            agent_cls=type(self),
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

    async def steer(self, message: str) -> None:
        """运行中插话：写入 steering 队列，由 agent_loop 在 step 边界注入为 user 事件（v2 promote 语义）。"""
        await self.message_queue.steer(message)

    async def save(self) -> None:
        pass

    @property
    def last_response(self) -> str:
        return self._last_assistant_text

    @property
    def aborted(self) -> bool:
        return self._aborted

    async def chat(self, user_message: str) -> None:
        await self._turn_runner.chat(user_message)

    # ── U3b 自动唤醒 ──

    def request_auto_wake(self, reason: str = "subagent_completed") -> bool:
        """synthetic 到达且本会话 idle → 调度自动唤醒轮（模型无人触发主动汇报）。

        跳过条件（返回 False）：子代理 / 已中止 / turn 运行中（消息列表按可见
        事件增量派生，运行中的轮次下一 step 自动看到通知，无需唤醒）/ 上一次
        唤醒尚未结束 / 预算耗尽（连续 auto_wake 轮无真实 user_message ≥3，
        v2 restart.ts:26-33 per-turn 预算思想）。
        """

        if self.is_sub_agent:
            return False
        if self._aborted or self._abort_event.is_set():
            return False
        if self._turn_lock.locked():
            return False
        if self._auto_wake_task is not None and not self._auto_wake_task.done():
            return False
        if auto_wake_budget_exhausted(self.session.events):
            return False
        self._auto_wake_task = asyncio.create_task(self._run_auto_wake(reason))
        return True

    async def _run_auto_wake(self, reason: str) -> None:
        await self._turn_runner.wake_turn(reason)

    async def run_once(self, prompt: str) -> dict:
        return await self._turn_runner.run_once(prompt)

    def _emit_text(self, text: str) -> None:
        text = safe_utf8_text(text)
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

    def build_tail_system_messages(
        self, tasks: list[task_store.TaskItem] | None = None) -> list[str]:
        # tasks = 每请求快照，由 ModelCaller._assemble_request 传下来（Plan 3b B1）。
        return prompt_runtime.build_tail_system_messages(self, tasks)

    def _refresh_runtime_system_prompt(self, force: bool = False) -> None:
        prompt_runtime.refresh_runtime_system_prompt(self, force=force)

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

    def _record_fold_event(self) -> None:
        self._compressor._record_fold_event()
        self._fold_last_time = self._compressor._fold_last_time
        self._fold_count = self._compressor._fold_count
        self._tool_error_streak = 0
        self._same_tool_repeat_count = 0
        self._last_tool_name = ""
        self._repeat_guard.reset()

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

    def append_tool_message(
        self,
        tool_call_id: str,
        content: str,
        tool_name: str = "",
        outcome: str = "",
    ) -> None:
        self._context_manager.append_tool_message(tool_call_id, content, tool_name, outcome)
        # 记录每个工具的结果字符数
        if tool_name:
            self._tool_result_chars[tool_name] = self._tool_result_chars.get(tool_name, 0) + len(content)

    def build_side_query(self, *, max_tokens: int = 2000):
        return self._build_side_query(max_tokens=max_tokens)

    def start_wiki_prefetch(self, user_message: str, side_query) -> None:
        run_wiki_prefetch(self, user_message, side_query)

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

    def execute_tool_call(self, name: str, inp: dict, assistant_seq: int | None = None):
        return self._tool_dispatcher.execute_tool_call(name, inp, assistant_seq=assistant_seq)

    def persist_large_result(self, tool_name: str, result: str) -> str:
        return self._persist_large_result(tool_name, result)

    def looks_like_tool_failure(self, tool_name: str, raw: str, result: str) -> bool:
        return self._looks_like_tool_failure(tool_name, raw, result)

    def clear_context_flag(self) -> None:
        self._context_cleared = False

    def refresh_runtime_system_prompt(self, force: bool = False) -> None:
        self._refresh_runtime_system_prompt(force=force)

    def _has_in_progress_task(
        self, tasks: list[task_store.TaskItem] | None = None) -> bool:
        """有没有任务正在执行 —— 折叠任务边界门的探针。

        读不出来一律返回 False：绝不能因为任务清单读不出来就不压缩，那会撑爆
        上下文窗口。失败方向刻意选在「照常折叠」这一侧，且**裁定权留在本方法里**
        （context_compressor 不 import task_store，只认一个返回 bool 的零参 callable）。

        交给压缩器的仍然是**零参** callable：见 check_and_compact。

        tasks = 每请求快照（Plan 3b Task B1）。有快照就读快照、一次盘都不碰；None
        表示「没有快照」（Agent._check_and_compact 那条不经 _attempt 的路径、直接
        调用它的测试、或快照本身加载失败），此时自己读一次并保留上面那条失败方向。
        刻意用 `is None` 而不是 falsy 判断：[] 是「读到了，确实没有任务」这个**答案**，
        把它当成「没有快照」会白读一次盘，还会把「store 读不出来」与「没有任务」
        两种状态混成一件事。
        """
        try:
            items = tasks if tasks is not None else task_store.list_tasks(self.session.id)
            return any(t.status == task_store.TASK_STATUS_IN_PROGRESS for t in items)
        except Exception as e:
            print_error(f"[fold_gate] task probe failed, folding normally: {e!r}")
            return False

    def check_and_compact(self, tasks: list[task_store.TaskItem] | None = None):
        # 折叠任务边界门（Plan 2 Task 4）：任务执行中途，工具结果/文件内容/报错
        # 是模型正在推理的活工作集，折叠它们不是省钱而是破坏当前任务。这里把门
        # 的探针一路传到 ContextCompressor._should_compress（经 context.py 纯中转），
        # 硬顶 min(threshold+0.10, 0.95) 是安全阀。
        #
        # 刻意交**零参 callable**而不是探针的返回值：check_and_compact 每次模型调用
        # 都跑，绝大多数时候利用率离任何触发点都很远、这一轮根本不做折叠决定，交值
        # 等于把那次判断变成无条件发生。解析发生在 ContextCompressor._resolve_defer_fold，
        # 只在逼近触发点时——这条惰性性质在加快照之后**不变**，变的只是探针读什么。
        #
        # 有快照时交一个闭包捕获快照的 lambda（仍然零参、仍然惰性），于是「逼近触发点
        # 才读盘」升级成「压根不读盘」：那一次读已经在 ModelCaller._attempt 顶部为
        # 三个消费者付过了。没有快照时交绑定方法本身，行为与加快照之前逐字相同。
        # `tasks is None` 而不是 falsy：[] 是一个真答案（没有任务 → 门不开），
        # 不该退化成一次多余的磁盘读。
        probe = (self._has_in_progress_task if tasks is None
                 else lambda: self._has_in_progress_task(tasks))
        return self._context_manager._check_and_compact(defer_fold=probe)

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

    async def compact(self) -> bool:
        return await self._context_manager.compact()

    def restore_session(self, data: dict) -> None:
        session_lifecycle.restore_agent_session(self, data)

    async def rewind_turns(self, n: int = 1) -> str:
        return await session_lifecycle.rewind_agent_turns(self, n)

    def fork_session(self) -> str:
        return session_lifecycle.fork_agent_session(self)

    def describe_context(self) -> list[dict]:
        return self._context_manager.describe_context()

    def delete_context_messages(self, indexes: list[int]) -> str:
        return self._context_manager.delete_context_messages(indexes)

    def keep_context_messages(self, indexes: list[int]) -> str:
        return self._context_manager.keep_context_messages(indexes)

    def _get_message_count(self) -> int:
        return self._context_manager._get_message_count()

    async def _check_and_compact(self)->None:
        # 与公开的 check_and_compact 同一条门：留一条不传 defer_fold 的路径，
        # 就等于给折叠门留了一个静默旁路。同样交探针本身（零参 callable）而不是
        # 它的返回值——理由见 check_and_compact。
        await self._context_manager._check_and_compact(
            defer_fold=self._has_in_progress_task)

    async def _compact_conversation(self, *, trigger: str = "manual")->bool:
        return await self._context_manager._compact_conversation(trigger=trigger)

    async def _compact_openai(self, *, trigger: str)->bool:
        return await self._context_manager._compact_openai(trigger=trigger)

    def _persist_large_result(self, tool_name: str, result: str) -> str:
        return persist_large_result(tool_name, result)

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
