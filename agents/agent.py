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
from agents.core.context_store import (
    ContextStore,
    cleared_placeholder,
    is_compressed_placeholder,
    snipped_placeholder,
)
from agents.memory.memory import MemoryPrefetch, start_memory_prefetch, format_memories_for_injection
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
from agents.core.session import save_folded_session_memory, save_session, Session
from agents.core.subagent import get_sub_agent_config
from agents.tools import ToolDef, tool_definitions, execute_tool, CONCURRENCY_SAFE_TOOLS, check_permission, \
    get_active_tool_definitions
from agents.logging import print_info, print_divider, print_assistant_text, print_sub_agent_start, print_sub_agent_end, \
    print_error, print_retry


# 指数退避重试


def _is_retryable(error: Exception) -> bool:
    status = getattr(error, "status_code", None) or getattr(error, "status", None)
    if status in (429, 503, 529):
        return True
    msg = str(error)
    if "overloaded" in msg or "ECONNRESET" in msg or "ETIMEDOUT" in msg:
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


#多层级压缩常数
SNIP_THRESHOLD = 0.60
SNIP_PLACEHOLDER = "[Content snipped - re-read if needed]"
SNIPPABLE_TOOLS = {"read_file", "grep_search", "list_files", "run_shell"}
MICROCOMPACT_IDLE_S = 5 * 60  # 5 minutes

KEEP_RECENT_RESULTS = 3



def _get_max_output_tokens(model: str) -> int:
    m = model.lower()
    if "opus-4-6" in m:
        return 64000
    if "sonnet-4-6" in m:
        return 32000
    if any(x in m for x in ("opus-4", "sonnet-4", "haiku-4")):
        return 32000
    return 16384

#转换tool的形式到openai
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
                 confirm_fn:Callable[[str], Awaitable[bool]] | None=None,
                 custom_system_prompt: str | None=None,
                 custom_tools: list[ToolDef] | None=None,
                 is_sub_agent: bool=False,
                 parent_abort_event: asyncio.Event | None=None,
                   session_id: str | None=None,
                   options: Any | None=None,):
        # 如果提供了 options，则从中提取参数
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
                confirm_fn = options.confirm_fn
                custom_system_prompt = options.custom_system_prompt
                custom_tools = options.custom_tools
                is_sub_agent = options.is_sub_agent
                parent_abort_event = options.parent_abort_event
                session_id = getattr(options, 'session_id', None)
        
        self.permission_mode = permission_mode
        self.thinking = thinking
        self.model = model
        self.is_sub_agent = is_sub_agent
        self.tools = custom_tools or tool_definitions
        self.max_cost_usd = max_cost_usd
        self.max_turns = max_turns
        self.confirm_fn = confirm_fn
        self._custom_system_prompt = custom_system_prompt
        # 保存连接信息，供模型注册表构造主端点、派生子 Agent 端点使用。
        self._api_base = api_base
        self._api_key = api_key
        # side query 独立端点客户端缓存：(cache_key, (client, model, use_openai))
        self._side_client_cache: tuple[tuple, tuple] | None = None
        # 上下文窗口统一用 token 计数。context_window 是模型完整窗口（默认 200k），
        # effective_window 预留输出余量后用于压缩阈值计算。
        from agents.config import get_endpoint_by_model
        _ep = get_endpoint_by_model(model)
        self.context_window = _ep.context_window if _ep else 200000
        self.effective_window = self.context_window - 20000
        self.auto_compact_threshold = _ep.auto_compact_threshold if _ep else 0.70
        self.session_id = session_id or uuid.uuid4().hex[:8]
        self.session_start_time= time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime())
        
        self.session = Session(self.session_id, origin="sub_agent" if is_sub_agent else None)
        self._current_turn: int = 0
        self._current_step: int = 0
        self._permission_waiters: dict[str, asyncio.Future] = {}
        
        if not is_sub_agent:
            from agents.observability.trace import set_trace_session, get_trace_session
            old_session = get_trace_session()
            set_trace_session(self.session_id)
            import logging
            logging.getLogger(__name__).info(f"[DEBUG] Agent.__init__ set trace session: {self.session_id} (was: {old_session})")
        
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.last_input_token_count = 0
        self.current_turns = 0
        self.last_api_call_time = 0

        # /goal 模式：最近一轮助手回复文本，供 verifier 作为证据。
        self._last_assistant_text = ""
        # ACE 可逆上下文：snip/clear 前原文无损存入，可用 context_restore 取回。
        self._context_store = ContextStore()

        # 提取的子模块
        from .core.context_compressor import ContextCompressor
        from .core.permission_gate import PermissionGate
        from .core.session_lifecycle import SessionLifecycle
        from .skills.skill_orchestrator import SkillOrchestrator
        self._compressor = ContextCompressor(
            self._context_store,
            auto_compact_threshold=self.auto_compact_threshold,
            effective_window=self.effective_window,
        )
        self._permission_gate = PermissionGate()
        self._session_lifecycle = SessionLifecycle(self._context_store)
        self._skill_orchestrator = SkillOrchestrator(
            side_query_fn=self._build_side_query,
            permission_mode=self.permission_mode,
            session_id=self.session_id,
            refresh_system_prompt=self._refresh_runtime_system_prompt,
        )

        self._aborted = False
        # 共享中止事件：父 Agent 触发 abort 时置位，子 Agent 在循环检查点读取，
        # 实现“父打断 → 子立刻退出”的传播，而不只依赖 asyncio 任务取消。
        self._abort_event = asyncio.Event()
        self._parent_abort_event = parent_abort_event
        #存储异步任务
        self._current_task:asyncio.Task | None = None
        #权限白名单
        self._confirmed_paths: set[str] = set()


        # 计划模式”（Plan Mode）状态的变量
        self._pre_plan_mode: str | None=None
        self._plan_file_path: str | None=None
        self._plan_approval_fn : Callable[[str], Awaitable[bool]] | None=None
        self._context_cleared : bool=False

        #思考模式
        self._thinking_mode = self._resolve_thinking_mode()

        #子agent的输出缓存
        self._output_buffer: list[str] | None=None
        self._turn_output_buffer: list[str] | None = None
        self._turn_thinking_buffer: list[str] | None = None
        self._turn_event_buffer: list[dict] | None = None
        
        self._current_sub_agent_id: str | None = None

        # 编辑前读取
        self._read_file_state: dict[str, float] ={}

        #MCP集成
        self._mcp_manager = McpManager()
        self._mcp_initialized = False

        #记忆回溯
        #记录每条 memory 最近一次被注入的轮次，用于冷却衰减：
        #冷却期内不再重复召回，冷却到期后允许重新召回（替代整会话封杀）。
        self._memory_surfaced_at: dict[str, int] = {}
        #当前对话轮次计数（每次 chat() +1）
        self._turn_number = 0
        #当前会话占用的字节数
        self._session_memory_bytes = 0

        #区分message的历史消息（现在从事件日志派生）
        self._folded_session_memories: list[dict[str, Any]] = []
        self._fold_last_time: float = 0.0
        self._fold_count: int = 0
        self._tool_error_streak: int = 0
        self._same_tool_repeat_count: int = 0
        self._last_tool_name: str = ""
        self._repeat_chain_key: str = ""
        self._repeat_chain_count: int = 0

        #构建系统提示词
        self._base_system_prompt = custom_system_prompt or build_system_prompt()

        if self.permission_mode == "plan":
            self._plan_file_path = self._generate_plan_file_path()
            self._system_prompt = self._base_system_prompt + self._build_plan_mode_prompt()
        else:
            self._system_prompt = self._base_system_prompt

        #初始化大模型客户端
        self._openai_client = openai.AsyncOpenAI(base_url=api_base, api_key=api_key)
        
        self._refresh_runtime_system_prompt()
        self._loop = AgentLoop(self)

    #判断返回模型的思考模式
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

    #生成一个用于保存 AI 计划（Plan）的 Markdown 文件的绝对路径。
    def _generate_plan_file_path(self) -> str:
        d = Path.home() / ".mycode" / "plans"
        d.mkdir(parents=True, exist_ok=True)
        return str(d / f"plan-{self.session_id}.md")

    def _build_plan_mode_prompt(self) -> str:
        return f"""

    # Plan Mode Active

    Plan mode is active. You MUST NOT make any edits (except the plan file below), run non-readonly tools, or make any changes to the system.

    ## Plan File: {self._plan_file_path}
    Write your plan incrementally to this file using write_file or edit_file. This is the ONLY file you are allowed to edit.

    ## Workflow
    1. **Explore**: Read code to understand the task. Use read_file, list_files, grep_search.
    2. **Design**: Design your implementation approach. Use the agent tool with type="plan" if the task is complex.
    3. **Write Plan**: Write a structured plan to the plan file including:
       - **Context**: Why this change is needed
       - **Steps**: Implementation steps with critical file paths
       - **Verification**: How to test the changes
    4. **Exit**: Call exit_plan_mode when your plan is ready for user review.

    IMPORTANT: When your plan is complete, you MUST call exit_plan_mode. Do NOT ask the user to approve — exit_plan_mode handles that."""

    #判断当前的任务所有的任务是否完成
    @property
    def is_processing(self)->bool:
        return self._current_task is not None and not self._current_task.done()

    #大模型调用的工厂方法,构建一个用于记忆召回（memory recall）的 sideQuery 可调用对象。
    def _get_side_client(self):
        """解析 side query 专用端点（MYCODE_SIDE_MODEL），返回 (client, model, use_openai)。

        未配置或解析结果与主端点完全一致时返回 None，表示复用主客户端。
        独立端点的客户端按 (model, base_url, protocol) 缓存，避免重复创建。
        """
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

        async def _sq_openai(system:str, user_message:str)->str:
            resp = await client.chat.completions.create(
                model=model,
                max_tokens=max(1, int(max_tokens)),
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user_message},
                ],

            )
            if not resp.choices:
                logging.warning("side_query returned no OpenAI-compatible choices: model=%s", model)
                return ""
            choice = resp.choices[0]
            content = choice.message.content or ""
            if not content.strip():
                logging.warning(
                    "side_query returned empty OpenAI-compatible response: model=%s finish_reason=%s message=%s",
                    model,
                    getattr(choice, "finish_reason", ""),
                    choice.message,
                )
            return content
        return _sq_openai
    #异步任务取消（Abort）
    def abort(self) -> None:
        self._aborted = True
        # 置位共享事件，让正在运行的子 Agent 在下个检查点感知到中止。
        self._abort_event.set()
        if self._current_task and not self._current_task.done():
            self._current_task.cancel()

    def _abort_requested(self) -> bool:
        """本 Agent 或任一父级是否已请求中止（用于循环检查点）。"""
        if self._aborted:
            return True
        if self._parent_abort_event is not None and self._parent_abort_event.is_set():
            return True
        return False

    def _primary_endpoint(self) -> ModelEndpoint:
        """把当前 Agent 的连接信息包装成主端点，供模型注册表路由使用。"""
        return ModelEndpoint(
            model=self.model,
            base_url=self._api_base,
            api_key=self._api_key,
            use_openai=True,
        )

    def _spawn_sub_agent(self, *, system_prompt: str, tools: list[ToolDef], model_ref: str, label: str) -> "Agent":
        """按模型注册表解析出子 Agent 应使用的端点，并构造子 Agent 实例。

        model_ref 为端点 ID 或裸模型名；为空则继承父端点。
        """
        endpoint = resolve_agent_endpoint(label, model_ref=model_ref, primary=self._primary_endpoint())
        return Agent(
            model=endpoint.model,
            api_base=endpoint.base_url,
            api_key=endpoint.api_key,
            custom_system_prompt=system_prompt,
            custom_tools=tools,
            is_sub_agent=True,
            permission_mode="plan" if self.permission_mode == "plan" else "bypassPermissions",
            parent_abort_event=self._abort_event,
        )

    def set_confirm_fn(self, fn:Callable[[str], Awaitable[bool]]) -> None:
        self.confirm_fn = fn

    def set_plan_approval_fn(self, fn:Callable[[str], Awaitable[bool]]) -> None:
        self._plan_approval_fn = fn


    #计划模式开关（“状态切换与现场保护”机制）
    def toggle_plan_mode(self) -> str:
        """
               1. 退出计划模式（从 plan 切回原模式）
               当当前模式已经是 plan 时，执行 if 分支：
               恢复之前的状态：self.permission_mode = self._pre_plan_mode or "default"。
                   在进入计划模式时，程序会把原本的模式保存在 _pre_plan_mode 里。退出时，就把它重新拿出来赋值回去，恢复到切换前的状态。
               清理计划模式的痕迹：把 _pre_plan_mode 和 _plan_file_path（计划文件路径）清空，并将系统提示词 _system_prompt 恢复为最基础的 _base_system_prompt。
               同步 OpenAI 消息：如果底层使用的是 OpenAI 接口，它还会同步更新消息列表里的第一条系统提示词，确保 AI 的上下文也跟着切换回来。
               反馈返回：打印退出提示，并返回恢复后的模式名称。

               2. 进入计划模式（从其他模式切入 plan）
       当当前模式不是 plan 时，执行 else 分支：
       保护当前现场：self._pre_plan_mode = self.permission_mode。先把当前正在使用的模式（比如正常模式或自动接受模式）暂存起来，方便以后能原路返回。
       切换并初始化：将当前模式设为 "plan"，生成一个专属的计划文件路径，并扩展系统提示词。通过拼接 _build_plan_mode_prompt()，给 AI 注入“只动脑不动手、输出结构化计划”的专属指令。
       同步 OpenAI 消息：同样地，如果使用 OpenAI，也会实时更新上下文里的系统提示词。
       反馈与返回：打印进入提示（包含计划文件的路径），并返回 "plan"。
        """
        if self.permission_mode == "plan":
            self.permission_mode = self._pre_plan_mode or "default"
            self._pre_plan_mode = None
            self._plan_file_path = None
            self._system_prompt = self._base_system_prompt
            self.session.system_prompt = self._system_prompt
            print_info(f"Exited plan mode -> {self.permission_mode} mode")
            return self.permission_mode
        else:
            self._pre_plan_mode = self.permission_mode
            self.permission_mode = "plan"
            self._plan_file_path = self._generate_plan_file_path()
            self._system_prompt = self._base_system_prompt + self._build_plan_mode_prompt()
            self.session.system_prompt = self._system_prompt
            print_info(f"Entered plan mode. Plan file: {self._plan_file_path}")
            return "plan"

    def get_token_usage(self) -> dict:
        return {"input":self.total_input_tokens, "output":self.total_output_tokens}

    # ── 公开方法（供 AgentService / 外部调用）──

    def get_messages(self) -> list[dict]:
        """获取 LLM 消息历史（从事件日志派生）。"""
        return self.session.get_messages_for_llm()

    def truncate_messages_to(self, index: int) -> None:
        """截断事件日志到指定索引。"""
        messages = self.session.get_messages_for_llm()
        if index >= len(messages):
            return
        # 找到对应的事件序号并截断
        msg_count = 0
        for i, event in enumerate(self.session._log):
            if event["type"] in ("user_message", "assistant_message", "tool_result_msg"):
                if msg_count >= index:
                    self.session._log = self.session._log[:i]
                    return
                msg_count += 1

    def set_permission_mode(self, mode: str) -> None:
        self.permission_mode = mode

    def steer(self, message: str) -> None:
        if not hasattr(self, '_steer_queue') or self._steer_queue is None:
            self._steer_queue = []
        self._steer_queue.append(message)

    async def save(self) -> None:
        await self._auto_save()

    @property
    def last_response(self) -> str:
        return self._last_assistant_text

    @property
    def aborted(self) -> bool:
        return self._aborted

    #主入口

    async def  chat(self, user_message:str)->None:
        print(f"[DEBUG] agent.chat: STARTED - self.session_id = {self.session_id}, is_sub_agent = {self.is_sub_agent}")
        #懒加载MCP服务在第一次chat的时候
        if not self._mcp_initialized and not self.is_sub_agent:
            print(f"[DEBUG] agent.chat: initializing MCP")
            self._mcp_initialized = True
            try:
                # 使用 asyncio.wait_for 添加总超时，避免 MCP 初始化卡住整个聊天
                await asyncio.wait_for(
                    self._mcp_manager.load_and_connect(),
                    timeout=30.0  # 总超时 30 秒
                )
                mcp_defs = self._mcp_manager.get_tool_definitions()
                if mcp_defs:
                    # 注册 MCP 工具到 Registry
                    from agents.tools.mcp_registry import global_registry
                    for mcp_def in mcp_defs:
                        # 从工具名中提取 server 名（格式：mcp__server__tool）
                        parts = mcp_def["name"].split("__")
                        if len(parts) >= 3:
                            server_name = parts[1]
                            global_registry.register_mcp(mcp_def, server=server_name)
                    # 同时更新 self.tools 以保持向后兼容
                    self.tools = self.tools + mcp_defs
            except asyncio.TimeoutError:
                from .observability.trace import trace_error
                trace_error("timeout", "MCP init timeout (30s)", operation="mcp_init")
                print_error("MCP init timeout (30s) - continuing without MCP tools")
            except Exception as e:
                from .observability.trace import trace_error
                trace_error(type(e).__name__, str(e), operation="mcp_init")
                print_error(f"MCP init failed: {e}")
            print(f"[DEBUG] agent.chat: MCP init done")

        # 跨会话知识复用：注入相关历史会话记忆
        print(f"[DEBUG] agent.chat: before cross_session_memory")
        from agents.config import load_config
        _app_cfg = load_config()
        if not self.is_sub_agent and _app_cfg.cross_session_memory:
            from agents.core.session_memory import search_folded_memories, format_folded_memories_for_injection
            related_memories = search_folded_memories(user_message, top_k=3)
            if related_memories:
                memory_context = format_folded_memories_for_injection(related_memories)
                # 追加到系统提示
                self._system_prompt += memory_context
                self.session.system_prompt = self._system_prompt

        original_user_message = _safe_utf8_text(user_message)
        ready_skill_extraction_window: dict[str, Any] | None = None
        self._skill_orchestrator.last_retrieved_skill_reference = None
        if not self.is_sub_agent:
            ready_skill_extraction_window = self._skill_orchestrator.pop_pending_extraction_window(
                original_user_message, self._tool_error_streak
            )
            user_message, ref = self._skill_orchestrator.augment_message(original_user_message)
            self._skill_orchestrator.last_retrieved_skill_reference = ref

        self._aborted = False
        # 新一轮对话开始时清除中止信号，避免上一轮的中止状态影响本轮。
        self._abort_event.clear()
        # 轮次计数递增，用于记忆召回冷却衰减。
        self._turn_number += 1
        self._turn_output_buffer = []
        self._turn_thinking_buffer = []
        self._turn_event_buffer = []
        
        self._current_turn += 1
        print(f"[DEBUG] agent.chat: before turn/start - self.session_id = {self.session_id}, self.session.id = {self.session.id}, is_sub_agent = {self.is_sub_agent}")
        if not self.is_sub_agent:
            print(f"[DEBUG] agent.chat: BEFORE turn/start - self.session_id = {self.session_id}, self.session.id = {self.session.id}, id(self.session) = {id(self.session)}")
        self.session.append("turn/start", {"turn": self._current_turn})
        # user_message 由 agent_loop._prepare_turn() 写入，这里不重复写入
        
        from .observability.trace import trace_event
        trace_kwargs: dict[str, Any] = {
            "turn": self._turn_number,
            "sub_agent": self.is_sub_agent,
            "user_preview": user_message[:200],
        }
        if not self.is_sub_agent:
            trace_kwargs["session"] = self.session_id
        trace_event("turn.start", **trace_kwargs)
        _turn_t0 = time.time()
        coro = self._chat_openai(user_message)
        self._current_task = asyncio.create_task(coro)
        try:
            await self._current_task
        except asyncio.CancelledError:
            self._aborted = True
            from .observability.trace import trace_error
            trace_error("cancelled", "Turn cancelled", operation="chat")
            self.session.append("turn/end", {"turn": self._current_turn, "reason": "aborted", "sub_agent_id": self._current_sub_agent_id})
            raise
        except Exception as e:
            from .observability.trace import trace_error, trace_event
            trace_error(type(e).__name__, str(e), operation="chat")
            print_error(f"[ERROR] {type(e).__name__}: {e}")
            trace_event(
                "turn.end",
                turn=self._turn_number,
                aborted=True,
                error=type(e).__name__,
                duration_s=round(time.time() - _turn_t0, 2),
            )
            self.session.append("turn/end", {"turn": self._current_turn, "reason": "error", "error": str(e), "sub_agent_id": self._current_sub_agent_id})
            return
        finally:
            self._current_task = None
        assistant_text = "".join(self._turn_output_buffer or []).strip()
        thinking_text = "".join(self._turn_thinking_buffer or []).strip()
        self._turn_output_buffer = None
        self._turn_thinking_buffer = None
        self._turn_event_buffer = None
        
        self.session.append("turn/end", {"turn": self._current_turn, "reason": "completed", "sub_agent_id": self._current_sub_agent_id})
        # Aggregated trace event for the turn (includes thinking and text content)
        trace_event(
            "turn.end",
            turn=self._turn_number,
            aborted=self._aborted,
            duration_s=round(time.time() - _turn_t0, 2),
            assistant_preview=assistant_text[:500],
            thinking_preview=thinking_text[:500] if thinking_text else None,
            sub_agent_id=self._current_sub_agent_id,
        )
        # /goal 模式的 verifier 需要最近一轮的助手报告作为证据。
        self._last_assistant_text = assistant_text
        if not self.is_sub_agent and not self._aborted:
            self._skill_orchestrator.schedule_background_task(
                self._skill_orchestrator.run_skill_usage_tracking(original_user_message, assistant_text),
                plan_mode=(self.permission_mode == "plan"),
            )
            
            # Cadence 门控：避免每轮都触发进化（减少冗余 side_query 调用）
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
                pass  # Ignore console errors in server environment
            await self._auto_save()

    async def run_once(self, prompt: str) -> dict:
        self._output_buffer = []
        prev_in = self.total_input_tokens
        prev_out = self.total_output_tokens
        await self.chat(prompt)
        text = "".join(self._output_buffer)
        self._output_buffer = None
        return {
            "text": text,
            "tokens": {
                "input": self.total_input_tokens - prev_in,
                "output": self.total_output_tokens - prev_out
            },
        }

    #输出工具：统一处理模型输出文本。根据当前是否处于“收集输出”的模式
    # 决定是把文本存进缓冲区，还是直接打印到终端。
    def _emit_text(self, text: str) -> None:
        text = _safe_utf8_text(text)
        if self._turn_output_buffer is not None:
            self._turn_output_buffer.append(text)
        if self._output_buffer is not None:
            self._output_buffer.append(text)
        else:
            print_assistant_text(text)
        
        # 流式事件只发送 SSE，不持久化到事件日志
        event_data = {"content": text, "turn": self._current_turn, "step": self._current_step}
        if self._current_sub_agent_id:
            event_data["sub_agent_id"] = self._current_sub_agent_id
        self.session.append("text", event_data)

    def _build_fold_guidance_section(self) -> str:
        if self._custom_system_prompt is not None:
            return ""
        utilization = self.last_input_token_count / self.effective_window if self.effective_window else 0.0
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
            return
        self._base_system_prompt = build_system_prompt()
        if self.permission_mode == "plan":
            self._system_prompt = self._base_system_prompt + self._build_plan_mode_prompt()
        else:
            self._system_prompt = self._base_system_prompt
        self._system_prompt += self._build_fold_guidance_section()
        self.session.system_prompt = self._system_prompt

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
        self.session._log.clear()
        self.session.system_prompt = self._system_prompt
        self._skill_orchestrator.reset()
        self._fold_last_time = 0.0
        self._fold_count = 0
        self._tool_error_streak = 0
        self._same_tool_repeat_count = 0
        self._last_tool_name = ""
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.last_input_token_count = 0
        print_info("Conversation cleared.")

    # ── AgentLoop 公开接口 ─────────────────────────────────────────────────────

    @property
    def messages(self) -> list[dict]:
        """获取 LLM 消息历史（从事件日志派生）。"""
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
    def plan_file_path(self) -> str | None:
        return self._plan_file_path

    @property
    def confirmed_paths(self) -> set[str]:
        return self._confirmed_paths

    @property
    def context_cleared(self) -> bool:
        return self._context_cleared

    @property
    def memory_prefetch(self):
        return getattr(self, '_memory_prefetch', None)

    def abort_requested(self) -> bool:
        return self._abort_requested()

    def mark_aborted(self) -> None:
        self._aborted = True

    def append_user_message(self, content: str) -> None:
        """追加用户消息到事件日志。"""
        self.session.append("user_message", {"content": content})

    def append_tool_message(self, tool_call_id: str, content: str) -> None:
        """追加工具结果消息到事件日志。"""
        self.session.append("tool_result_msg", {"call_id": tool_call_id, "content": content})

    def reset_repeat_chain(self) -> None:
        self._repeat_chain_key = ""
        self._repeat_chain_count = 0

    def build_side_query(self):
        return self._build_side_query()

    def start_memory_prefetch(self, user_message: str, side_query) -> None:
        from agents.memory.memory import start_memory_prefetch
        self._memory_prefetch = start_memory_prefetch(
            user_message, side_query,
            self._cooled_memory_paths(), self._session_memory_bytes,
        )

    def record_memory_surface(self, path: str, content_bytes: int) -> None:
        self._memory_surfaced_at[path] = self._turn_number
        self._session_memory_bytes += content_bytes

    def add_input_tokens(self, count: int) -> None:
        self.total_input_tokens += count

    def add_output_tokens(self, count: int) -> None:
        self.total_output_tokens += count

    def set_last_input_tokens(self, count: int) -> None:
        self.last_input_token_count = count

    def increment_turns(self) -> None:
        self.current_turns += 1

    def check_budget(self) -> dict:
        return self._check_budget()

    def publish_stream_event(self, event: dict) -> None:
        event_type = event.pop("type", "info")
        self.session.append(event_type, event)

    def publish_tool_call_event(self, call_id: str, name: str, inp: dict) -> None:
        from agents.observability.trace import trace_event
        # 流式事件只发送 SSE，不持久化到事件日志
        event_data = {"call_id": call_id, "name": name, "input": inp, "turn": self._current_turn, "step": self._current_step}
        if self._current_sub_agent_id:
            event_data["sub_agent_id"] = self._current_sub_agent_id
        self.session.append("tool_call", event_data)
        trace_event("stream.tool_call", call_id=call_id, name=name, sub_agent_id=self._current_sub_agent_id)

    def publish_tool_result_event(self, call_id: str, name: str, result: str, status: str) -> None:
        from agents.observability.trace import trace_event
        # 流式事件只发送 SSE，不持久化到事件日志
        event_data = {"call_id": call_id, "name": name, "result": result, "status": status, "turn": self._current_turn, "step": self._current_step}
        if self._current_sub_agent_id:
            event_data["sub_agent_id"] = self._current_sub_agent_id
        self.session.append("tool_result", event_data)
        
        # tool_result_msg 是聚合事件，需要持久化（但这里不写入，由 agent_loop 调用 append_tool_message 写入）
        
        trace_event("stream.tool_result", call_id=call_id, name=name, sub_agent_id=self._current_sub_agent_id)

    def record_tool_outcome(self, tool_name: str, success: bool) -> None:
        self._record_tool_outcome(tool_name, success)

    def set_current_tool_name(self, name: str | None) -> None:
        self._current_tool_name = name

    def add_confirmed_path(self, path: str) -> None:
        self._confirmed_paths.add(path)

    def execute_tool_call(self, name: str, inp: dict):
        return self._execute_tool_call(name, inp)

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

    def run_compression_pipeline(self) -> None:
        self._run_compression_pipeline()

    def check_and_compact(self):
        return self._check_and_compact()

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
        """等待所有后台 Skill 任务完成。"""
        await self._skill_orchestrator.drain_background_tasks()

    async def extract_now(self, hint: str = "") -> dict[str, Any]:
        """立即执行 Skill 提取（交互式）。"""
        return await self._skill_orchestrator.extract_now(hint, confirm_fn=self.confirm_fn)

    def show_cost(self):
        total = self._get_current_cost_usd()
        budget_info = f" / ${self.max_cost_usd} budget" if self.max_cost_usd else ""
        turn_info = f" | Turns: {self.current_turns}/{self.max_turns}" if self.max_turns else ""
        print_info(
            f"Tokens: {self.total_input_tokens} in / {self.total_output_tokens} out\n  Estimated cost: ${total:.4f}{budget_info}{turn_info}")

    def status_line(self) -> str:
        """REPL 提示符上方的状态行：模型名 + token 累计 + 上下文利用率。

        利用率用最近一次 API 调用的 input tokens / effective_window，
        方便用户判断何时该 /compact。注意：这里全部是 token 数，
        与 /context 表格的字符数（chars）是不同单位。

        尚未发生任何 API 调用时（首轮提示符、/compact 之后、/clear 之后），
        真实上下文已含系统提示词+记忆+skills，但精确 token 数未知（API 还没
        上报过），此时显示 "-" 而不是误导性的 0。
        """
        if self.last_input_token_count > 0:
            util = self.last_input_token_count / self.effective_window if self.effective_window else 0.0
            ctx_part = f"ctx: {self.last_input_token_count}/{self.context_window} tokens ({util:.0%})"
        else:
            ctx_part = f"ctx: -/{self.context_window} tokens (未知，待首次调用)"
        return (
            f"model: {self.model} | {ctx_part} | session: {self.total_input_tokens} in / {self.total_output_tokens} out"
        )

    #获取当前的花费，
    def _get_current_cost_usd(self) -> float:
        return (self.total_input_tokens / 1_000_000) * 3 + (self.total_output_tokens / 1_000_000) * 15

    #检查预算
    def _check_budget(self) -> dict:
        if self.max_cost_usd is not None and self._get_current_cost_usd() >= self.max_cost_usd:
            return {"exceeded": True, "reason": f"Cost limit reached (${self._get_current_cost_usd():.4f} >= ${self.max_cost_usd})"}
        if self.max_turns is not None and self.current_turns >= self.max_turns:
            return {"exceeded": True, "reason": f"Turn limit reached ({self.current_turns} >= {self.max_turns})"}
        return {"exceeded": False}

    #压缩会话
    async def compact(self)->None:
        compacted = await self._compact_conversation(trigger="manual")
        if not compacted:
            print_info("Nothing to compact yet.")


    #恢复会话信息
    def restore_session(self, data:dict)->None:
        from .core.session_lifecycle import SessionState
        state = SessionState(
            session_id=self.session_id,
            model=self.model,
            context_store=self._context_store,
        )
        self._session_lifecycle.restore(state, data, self.session)
        print_info(f"Session restored ({self._get_message_count()} messages).")

    #/rewind：回退对话 N 轮，同时把被修改的文件恢复到对应轮次开始时的状态。
    def rewind(self, n: int = 1) -> str:
        return self._session_lifecycle.rewind(self.session, self._read_file_state, n)

    # ── 三阶段恢复 ──
    
    def stage_revert(self, target_seq: int) -> dict:
        """Stage：计算恢复计划，预览变更。"""
        return self.session.stage_revert(target_seq)
    
    def clear_revert(self, current_snapshot: list[dict]) -> dict:
        """Clear：取消恢复，恢复到原始状态。"""
        return self.session.clear_revert(current_snapshot)
    
    def commit_revert(self, target_seq: int) -> dict:
        """Commit：确认恢复。"""
        return self.session.commit_revert(target_seq)

    #/fork：从当前会话创建一个完全相同的分支（新 session_id），并切换过去。
    def fork_session(self) -> str:
        from .core.session_lifecycle import SessionState
        state = SessionState(
            session_id=self.session_id,
            model=self.model,
            context_store=self._context_store,
            start_time=self.session_start_time,
        )
        result, new_session = self._session_lifecycle.fork(state, self.session)
        self.session = new_session
        self.session_id = new_session.id
        return result

    #/context：返回上下文描述行（index/role/label/chars），供 REPL 渲染表格。
    def describe_context(self) -> list[dict]:
        return self._session_lifecycle.describe(self.session.get_messages_for_llm())

    #/ctx del N [N2 ...]：删除指定 index 所属的消息组（保持工具配对完整）。
    def delete_context_messages(self, indexes: list[int]) -> str:
        return self._session_lifecycle.delete_messages(self.session, indexes)

    #/ctx keep N [N2 ...]：只保留指定组（+ system prompt），其余删除。
    def keep_context_messages(self, indexes: list[int]) -> str:
        return self._session_lifecycle.keep_messages(self.session, indexes)

    def _get_message_count(self) -> int:
        return len(self.session.get_messages_for_llm())

    # 记忆注入后的冷却轮数：冷却期内同一条 memory 不再参与召回，
    # 到期后自动解禁（替代旧的"整会话封杀"，避免长会话中记忆永久失效）。
    MEMORY_RECALL_COOLDOWN_TURNS = 5

    def _cooled_memory_paths(self) -> set[str]:
        """返回仍在冷却期内的 memory 路径集合。"""
        return {
            path for path, turn in self._memory_surfaced_at.items()
            if self._turn_number - turn < self.MEMORY_RECALL_COOLDOWN_TURNS
        }

    async def _auto_save(self) -> None:
        if self.is_sub_agent:
            return
        try:
            from .core.session_lifecycle import SessionState
            state = SessionState(
                session_id=self.session_id,
                model=self.model,
                context_store=self._context_store,
                start_time=self.session_start_time,
            )
            await asyncio.to_thread(self._session_lifecycle._save_state, state, self.session)
        except Exception:
            pass

    #自动压缩
    async def _check_and_compact(self)->None:
        await self._compressor.check_and_compact(
            self.session.get_messages_for_llm(),
            self.last_input_token_count,
            self._build_side_query(max_tokens=6000),
            self.session_id,
            self._folded_session_memories,
            self._refresh_runtime_system_prompt,
            self._get_message_count,
        )

    async def _compact_conversation(self, *, trigger: str = "manual")->bool:
        compacted = await self._compressor.compact(
            self.session.get_messages_for_llm(),
            trigger,
            self._build_side_query(max_tokens=6000),
            self.session_id,
            self._folded_session_memories,
            self._refresh_runtime_system_prompt,
            self._get_message_count,
        )
        if compacted:
            from .observability.trace import trace_event
            trace_event(
                "compact",
                trigger=trigger,
                messages_after=self._get_message_count(),
                ctx_tokens=self.last_input_token_count,
            )
            print_info("Conversation compacted.")
            self.last_input_token_count = 0
        return compacted

    async def _compact_openai(self, *, trigger: str)->bool:
        return await self._compact_conversation(trigger=trigger)

    async def _generate_folded_session_memory(self, transcript: str) -> dict[str, Any]:
        side_query = self._build_side_query(max_tokens=6000)
        return await self._compressor._generate_folded_memory(transcript, side_query)

    async def _record_folded_session_memory(self, trigger: str, memory: dict[str, Any]) -> None:
        await self._compressor._record_folded_memory(trigger, memory, self.session_id, self._folded_session_memories)

    #多层级压缩流水线
    def _run_compression_pipeline(self)->None:
        self._compressor.run_pipeline(self.session.get_messages_for_llm(), self.last_input_token_count, self.last_api_call_time)

    #第一层级压缩，预算压缩
    def _budget_tool_results_openai(self)->None:
        self._compressor._budget(self.session.get_messages_for_llm(), self.last_input_token_count)

    #第二级策略：修剪过期的工具执行结果
    def _snip_stale_results_openai(self) -> None:
        self._compressor._snip(self.session.get_messages_for_llm(), self.last_input_token_count)

    #微压缩
    def _microcompact_openai(self) -> None:
        self._compressor._microcompact(self.session.get_messages_for_llm(), self.last_api_call_time)

    #大结果持久化
    #如果工具返回的结果太大（超过 30KB），不要硬塞进上下文里，而是把它存成一个临时文件。
    # 然后在对话里只留一个‘文件路径’和‘内容预览’。如果模型后面还需要看完整内容，它可以再次调用工具去读取这个文件

    def _persist_large_result(self, tool_name: str, result: str) -> str:
        return persist_large_result(tool_name, result)

    #执行工具入口

    @staticmethod
    def _tool_timeout(name: str) -> int:
        if name == "run_shell":
            return 300
        if name in ("agent", "skill"):
            return 300
        if name in ("read_file", "grep_search", "list_files"):
            return 30
        return 60

    async def _execute_tool_call(self, name: str, inp: dict) -> str:
        from .observability.trace import trace_span
        _tool_t0 = time.time()
        trace_attrs = {"tool": name}
        if self._current_sub_agent_id:
            trace_attrs["sub_agent_id"] = self._current_sub_agent_id
        timeout = self._tool_timeout(name)
        with trace_span("tool_call", **trace_attrs) as span:
            try:
                result = await asyncio.wait_for(
                    self._execute_tool_call_inner(name, inp),
                    timeout=timeout,
                )
                span.set_attribute("success", True)
                span.set_attribute("duration_s", round(time.time() - _tool_t0, 2))
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
        if name == "compact_context":
            return await self._execute_compact_context_tool(inp)
        if name == "context_restore":
            return self._execute_context_restore_tool(inp)
        if name in ("enter_plan_mode", "exit_plan_mode"):
            return await self._execute_plan_mode_tool(name)
        if name == "agent":
            return await self._execute_agent_tool(inp)
        if name == "skill":
            return await self._execute_skill_tool(inp)
            # Route MCP tool calls to the MCP manager
        if self._mcp_manager.is_mcp_tool(name):
            return await self._mcp_manager.call_tool(name, inp)
        result = await execute_tool(name, inp, self._read_file_state)
        if name in {"skill_create", "skill_evolve"}:
            try:
                parsed = json.loads(result)
                if isinstance(parsed, dict) and parsed.get("ok"):
                    self._refresh_runtime_system_prompt()
            except Exception:
                pass
        return result

    async def _execute_compact_context_tool(self, inp: dict) -> str:
        reason = str(inp.get("reason") or "").strip()
        compacted = await self._compact_conversation(trigger="tool")
        if not compacted:
            self._record_tool_outcome("compact_context", False)
            return "No context compaction was performed because there is not enough conversation history yet."
        self._record_tool_outcome("compact_context", True)
        self._context_cleared = True
        suffix = f"\nReason: {reason}" if reason else ""
        return (
            "Context compacted into structured session memory. "
            "Continue from the folded memory now present in the conversation context."
            f"{suffix}"
        )

    def _execute_context_restore_tool(self, inp: dict) -> str:
        """ACE 可逆恢复：按 key 从 ContextStore 取回被 snip/clear 的原文。"""
        key = str(inp.get("key") or "").strip()
        if not key:
            return "Error: 'key' is required. Find it inside the snipped/cleared placeholder text."
        raw = self._context_store.get_raw(key)
        if raw is None:
            available = self._context_store.available_keys()
            hint = f" Available keys: {', '.join(available[:20])}" if available else " No restorable content is stored."
            return f"Error: no restorable content for key '{key}'.{hint}"
        return raw


    async def _execute_skill_tool(self, inp: dict) -> str:
        from .skills.skills import execute_skill
        result = execute_skill(inp.get("skill_name", ""), inp.get("args", ""))

        if not result:
            return f"Unknown skill: {inp.get('skill_name', '')}"

        #fork 表示这个 skill 不直接把 prompt 塞回当前对话，而是要启动一个子 Agent 单独完成任务。
        if result["context"] == "fork":
            # result["allowed_tools"] - 直接访问
            tools = (
                [t for t in self.tools if t["name"] in  result["allowed_tools"] ]
                #result.get("allowed_tools") - 安全访问
                # 存在key：返回对应的值（可能是 None、[]、["tool1"] 等）
                # 不存在key：返回 None（不会抛异常）
                if result.get("allowed_tools")
                else  [t for t in self.tools if t["name"] != "agent"]
            )

            print_sub_agent_start("skill-fork", inp.get("skill_name", ""))
            sub_agent = self._spawn_sub_agent(
                system_prompt=result["prompt"],
                tools=tools,
                model_ref=str(result.get("model") or ""),
                label="skill-fork",
            )
            try:
                sub_result = await sub_agent.run_once(inp.get("args") or "Execute this skill task.")
                self.total_input_tokens += sub_result["tokens"]["input"]
                self.total_output_tokens += sub_result["tokens"]["output"]
                print_sub_agent_end("skill-fork", inp.get("skill_name", ""))
                return sub_result["text"] or "(Skill produced no output)"
            except Exception as e:
                print_sub_agent_end("skill-fork", inp.get("skill_name", ""))
                return f"Skill fork error: {e}"

        return f'[Skill "{inp.get("skill_name", "")}" activated]\n\n{result["prompt"]}'

    async def _execute_plan_mode_tool(self, name):
        if name == "enter_plan_mode":
            if self.permission_mode == "plan":
                return "Already in plan mode."
            self._pre_plan_mode = self.permission_mode
            self.permission_mode = "plan"
            self._plan_file_path =  self._generate_plan_file_path()
            self._system_prompt = self._base_system_prompt + self._build_plan_mode_prompt()
            self.session.system_prompt = self._system_prompt
            print_info("Entered plan mode (read-only). Plan file: " + self._plan_file_path)
            return f"Entered plan mode. You are now in read-only mode.\n\nYour plan file: {self._plan_file_path}\nWrite your plan to this file. This is the only file you can edit.\n\nWhen your plan is complete, call exit_plan_mode."
        if name == "exit_plan_mode":
            if self.permission_mode != "plan":
                return "Not in plan mode."
            plan_content = "(No plan file found)"
            if self._plan_file_path and Path(self._plan_file_path).exists():
                plan_content = self._plan_file_path
            # 交互式审批流程（如果有审批函数）
            if self._plan_approval_fn:
                result = self._plan_approval_fn(plan_content)
                choice = result.get("choice", "manual-execute")

                if choice =="keep-planning":
                    feedback = result.get("feedback") or "Please revise the plan."
                    return (
                        f"User rejected the plan and wants to keep planning.\n\n"
                        f"User feedback: {feedback}\n\n"
                        f"Please revise your plan based on this feedback. When done, call exit_plan_mode again."
                    )

                if choice == "clear-and-execute":
                    target_mode = "acceptEdits"
                elif choice == "execute":
                    target_mode = "acceptEdits"
                else:  # manual-execute
                    target_mode = self._pre_plan_mode or "default"

                self.permission_mode = target_mode
                self._pre_plan_mode = None
                saved_plan_path = self._plan_file_path
                self._plan_file_path = None
                self._system_prompt = self._base_system_prompt
                self.session.system_prompt = self._system_prompt

                if choice == "clear-and-execute":
                    self._clear_history_keep_system()
                    self._context_cleared = True
                    print_info(f"Plan approved. Context cleared, executing in {target_mode} mode.")
                    return (
                        f"User approved the plan. Context was cleared. Permission mode: {target_mode}\n\n"
                        f"Plan file: {saved_plan_path}\n\n"
                        f"## Approved Plan:\n{plan_content}\n\n"
                        f"Proceed with implementation."
                    )
                print_info(f"Plan approved. Executing in {target_mode} mode.")
                return (
                    f"User approved the plan. Permission mode: {target_mode}\n\n"
                    f"## Approved Plan:\n{plan_content}\n\n"
                    f"Proceed with implementation."
                )
            # 没有审批函数时的回退（例如子代理）
            self.permission_mode = self._pre_plan_mode or "default"
            self._pre_plan_mode = None
            self._plan_file_path = None
            self._system_prompt = self._base_system_prompt
            self.session.system_prompt = self._system_prompt

            print_info("Exited plan mode. Restored to " + self.permission_mode + " mode.")
            return f"Exited plan mode. Permission mode restored to: {self.permission_mode}\n\n## Your Plan:\n{plan_content}"

        return f"Unknown plan mode tool: {name}"

    def _clear_history_keep_system(self) -> None:
        """清空历史信息，但是保留系统prompt."""
        self.session._log.clear()
        self.session.system_prompt = self._system_prompt
        self.last_input_token_count = 0
        self._fold_last_time = 0.0
        self._fold_count = 0
        self._tool_error_streak = 0
        self._same_tool_repeat_count = 0
        self._last_tool_name = ""

    async def _execute_agent_tool(self, inp: dict) -> str:
        agent_type = inp.get("type", "general")
        description = inp.get("description", "sub-agent task")
        prompt = inp.get("prompt", "")
        print_sub_agent_start(agent_type, description)
        
        import uuid
        sub_agent_id = str(uuid.uuid4())[:8]
        
        # 创建独立的子智能体 Session
        from agents.core.session import Session
        sub_session = Session(
            session_id=sub_agent_id,
            parent_session=self.session_id,
            origin="sub_agent",
            agent_type=agent_type,
        )
        
        self.session.append("sub_agent/start", {
            "agent_id": sub_agent_id,
            "agent_type": agent_type,
            "description": description,
            "sub_session_id": sub_session.id,
        })
        from .observability.trace import trace_event
        trace_event("stream.sub_agent_start", agent_id=sub_agent_id, agent_type=agent_type, description=description)

        config = get_sub_agent_config(agent_type)

        sub_agent = self._spawn_sub_agent(
            system_prompt=config["system_prompt"],
            tools=config["tools"],
            model_ref=config.get("model_ref", ""),
            label=agent_type,
        )
        
        # 子智能体使用独立的 Session
        sub_agent.session = sub_session
        sub_agent.session_id = sub_session.id
        sub_agent._current_sub_agent_id = sub_agent_id
        
        start_time = time.time()
        try:
            result = await sub_agent.run_once(prompt)
            self.total_input_tokens += result["tokens"]["input"]
            self.total_output_tokens += result["tokens"]["output"]
            print_sub_agent_end(agent_type, description)
            self.session.append("sub_agent/end", {
                "agent_id": sub_agent_id,
                "status": "completed",
                "summary": (result["text"] or "")[:200],
                "duration_ms": int((time.time() - start_time) * 1000),
                "sub_session_id": sub_session.id,
            })
            trace_event("stream.sub_agent_end", agent_id=sub_agent_id, agent_type=agent_type, status="completed")
            if sub_agent._aborted:
                return "(Sub-agent aborted)"
            return result["text"] or "(Sub-agent produced no output)"
        except Exception as e:
            print_sub_agent_end(agent_type, description)
            self.session.append("sub_agent/end", {
                "agent_id": sub_agent_id,
                "status": "error",
                "summary": str(e),
                "duration_ms": int((time.time() - start_time) * 1000),
                "sub_session_id": sub_session.id,
            })
            trace_event("stream.sub_agent_end", agent_id=sub_agent_id, agent_type=agent_type, status="error", error=str(e))
            return f"Sub-agent error: {e}"

    #openAI后端

    async def _chat_openai(self, user_message: str) -> None:
        await self._loop.run(user_message)

    async def _call_openai_stream(self) -> dict:
        return await self._loop.call_model_stream()

    async def _confirm_dangerous(self, command: str) -> bool:
        self._permission_gate.set_session(self.session)
        self._permission_gate.set_confirm_fn(self.confirm_fn) if self.confirm_fn else None
        self._permission_gate.set_sub_agent_id(self._current_sub_agent_id)
        self._permission_gate.set_abort_fn(self._abort_requested)
        self._permission_gate.set_current_tool_name(getattr(self, '_current_tool_name', 'unknown'))
        return await self._permission_gate.confirm(command)
    
    async def request_permission(self, request_id: str, command: str, tool_name: str, 
                                  message: str = "", sub_agent_id: str | None = None,
                                  timeout: float = 300.0) -> bool:
        """请求权限确认。通过 session 事件发送请求，等待响应。"""
        rpc_id = str(uuid.uuid4())
        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._permission_waiters[rpc_id] = future
        
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
        """响应权限请求。"""
        future = self._permission_waiters.pop(rpc_id, None)
        if not future:
            return False
        future.set_result({"allowed": allowed})
        return True
    
    def set_permission_response(self, request_id: str, allowed: bool) -> None:
        self._permission_gate.set_response(request_id, allowed)
