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
from agents.memory.memory import MemoryPrefetch, start_memory_prefetch, format_memories_for_injection
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
from agents.core.session import save_folded_session_memory, save_session, Session
from agents.core.subagent import get_sub_agent_config
from agents.tools import ToolDef, tool_definitions, execute_tool, CONCURRENCY_SAFE_TOOLS, check_permission, \
    get_active_tool_definitions
from agents.logging import print_info, print_divider, print_assistant_text, print_sub_agent_start, print_sub_agent_end, \
    print_error, print_retry


class ContentLevelError(Exception):
    """模型返回内容级错误（空响应、截断、畸形 tool_call 等）。"""
    pass


# 指数退避重试


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


#多层级压缩常数
SNIP_THRESHOLD = 0.60
SNIP_PLACEHOLDER = "[Content snipped - re-read if needed]"
SNIPPABLE_TOOLS = {"read_file", "outline_file", "grep_search", "list_files", "run_shell"}
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
                 workspace: Any = None,
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
                workspace = options.workspace
                session_id = getattr(options, 'session_id', None)
        
        self.permission_mode = permission_mode
        self.thinking = thinking
        self.model = model
        self.is_sub_agent = is_sub_agent
        self.tools = custom_tools if custom_tools is not None else tool_definitions
        self.max_cost_usd = max_cost_usd
        self.max_turns = max_turns
        self.confirm_fn = confirm_fn
        self._custom_system_prompt = custom_system_prompt
        # 会话工作区（显式单一来源；未指定时回退进程 CWD，仅 CLI 场景）
        self.workspace: Path = Path(workspace).resolve() if workspace else Path.cwd()
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
        self._user_message_written_this_turn: bool = False
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

        # 提取的子模块
        from .core.context_compressor import ContextCompressor
        from .core.permission_gate import PermissionGate
        from .core.session_lifecycle import SessionLifecycle
        from .skills.skill_orchestrator import SkillOrchestrator
        self._compressor = ContextCompressor(
            effective_window=self.effective_window,
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

        # Wiki 召回状态
        self._wiki_surfaced_at: dict[str, int] = {}
        self._wiki_prefetch: asyncio.Task | None = None
        self._wiki_prefetch_consumed = False

        #区分message的历史消息（现在从事件日志派生）
        self._folded_session_memories: list[dict[str, Any]] = []
        self._fold_last_time: float = 0.0
        self._fold_count: int = 0
        self._tool_error_streak: int = 0
        self._same_tool_repeat_count: int = 0
        self._last_tool_name: str = ""
        self._repeat_chain_key: str = ""
        self._repeat_chain_count: int = 0

        #构建系统提示词（workspace 作用域内：项目级技能/配置按本会话工作区解析）
        from .core.workspace import set_workspace, reset_workspace
        _ws_token = set_workspace(self.workspace)
        try:
            self._base_system_prompt = custom_system_prompt or build_system_prompt()

            if self.permission_mode == "plan":
                self._plan_file_path = self._generate_plan_file_path()
                self._system_prompt = self._base_system_prompt + self._build_plan_mode_prompt()
                print(f"[DEBUG] Agent.__init__: Entered plan mode. Plan file: {self._plan_file_path}")
                print(f"[DEBUG] Agent.__init__: System prompt contains 'Plan Mode': {'Plan Mode' in self._system_prompt}")
            else:
                self._system_prompt = self._base_system_prompt

            # 初始化 Rewind（如果启用）
            from agents.observability.rewind import init_rewind
            init_rewind()
            
            #初始化大模型客户端
            self._openai_client = openai.AsyncOpenAI(base_url=api_base, api_key=api_key)
            
            self._refresh_runtime_system_prompt()
        finally:
            reset_workspace(_ws_token)
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
    1. **Assess**: 理解用户需求，必要时用 ask_user 确认。调研现有代码，明确需求边界。
    2. **Write Plan**: 按以下三区域格式写入 plan 文件：

    ```markdown
    <!-- SPEC START -->
    # Spec: {{feature_name}}

    ## 动机
    为什么需要这个功能？

    ## 需求
    - 需求 1
    - 需求 2

    ## 约束
    - 约束 1

    ## 验收标准
    - [ ] 标准 1
    - [ ] 标准 2

    ## 非目标
    - 不做什么
    <!-- SPEC END -->

    <!-- PLAN START -->
    # Plan: {{feature_name}}

    ## 技术方案
    实现方案概述。

    ## 实施步骤
    1. 步骤 1
    2. 步骤 2
    <!-- PLAN END -->

    <!-- TASKS START -->
    ## Tasks

    ### Task 1: 任务标题
    - **文件**: `path/to/file.py`
    - **函数**: `function_name()`
    - **接口**: `def function_name(param: str) -> bool`
    - **验收**: `pytest tests/test_file.py -v`
    - **状态**: [ ] pending
    <!-- TASKS END -->
    ```

    3. **Exit**: Call exit_plan_mode when your plan is ready for user review.

    ## 追加模式
    如果 plan 文件已存在，先 read_file 读取现有内容。保留 SPEC 和 PLAN 区域不变，在 TASKS 区域末尾追加新的 tasks。

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
            workspace=self.workspace,
        )

    def set_confirm_fn(self, fn:Callable[[str], Awaitable[bool]]) -> None:
        self.confirm_fn = fn

    def set_plan_approval_fn(self, fn:Callable[[str], Awaitable[bool]]) -> None:
        self._plan_approval_fn = fn


    def _enter_plan_mode_internal(self) -> None:
        """内部方法：进入 plan 模式的统一入口。"""
        from .observability.trace import trace_span
        
        if self.permission_mode == "plan":
            return
        
        self._pre_plan_mode = self.permission_mode
        self.permission_mode = "plan"
        self._plan_file_path = self._generate_plan_file_path()
        self._system_prompt = self._base_system_prompt + self._build_plan_mode_prompt()
        self.session.system_prompt = self._system_prompt
        print_info("Entered plan mode (read-only). Plan file: " + self._plan_file_path)
        
        with trace_span("plan_mode.enter", 
            langfuse_observation_type="chain",
            plan_file_path=self._plan_file_path or "",
            previous_mode=self._pre_plan_mode or "",
        ):
            pass

    def _exit_plan_mode_internal(self) -> None:
        """内部方法：退出 plan 模式的统一入口。"""
        if self.permission_mode != "plan":
            return
        
        self.permission_mode = self._pre_plan_mode or "default"
        self._pre_plan_mode = None
        self._plan_file_path = None
        self._system_prompt = self._base_system_prompt
        self.session.system_prompt = self._system_prompt
        print_info(f"Exited plan mode -> {self.permission_mode} mode")

    def toggle_plan_mode(self) -> str:
        """切换 plan 模式。"""
        if self.permission_mode == "plan":
            self._exit_plan_mode_internal()
        else:
            self._enter_plan_mode_internal()
        return self.permission_mode

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
            if event["type"] in ("user_message", "assistant_message", "tool_result_msg", "memory_injection"):
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
        # 回合级 workspace 作用域：工具/技能/快照等深层消费方经 get_workspace() 读取
        from .core.workspace import set_workspace, reset_workspace
        _ws_token = set_workspace(self.workspace)
        try:
            await self._chat_inner(user_message)
        finally:
            reset_workspace(_ws_token)

    async def _chat_inner(self, user_message:str)->None:
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
        # 提前设置 session_id，确保 skill.recall span 能继承
        if not self.is_sub_agent:
            from .observability.tracer import set_current_session_id
            set_current_session_id(self.session_id)
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
        self._user_message_written_this_turn = False
        
        self._current_turn += 1
        print(f"[DEBUG] agent.chat: before turn/start - self.session_id = {self.session_id}, self.session.id = {self.session.id}, is_sub_agent = {self.is_sub_agent}")
        if not self.is_sub_agent:
            print(f"[DEBUG] agent.chat: BEFORE turn/start - self.session_id = {self.session_id}, self.session.id = {self.session.id}, id(self.session) = {id(self.session)}")
        self.session.append("turn/start", {"turn": self._current_turn})
        # user_message 由 agent_loop._prepare_turn() 写入，这里不重复写入
        
        from .observability.trace import trace_event, trace_span
        trace_kwargs: dict[str, Any] = {
            "turn": self._turn_number,
            "sub_agent": self.is_sub_agent,
            "user_preview": user_message[:200],
        }
        if not self.is_sub_agent:
            trace_kwargs["session"] = self.session_id

        _turn_t0 = time.time()
        _turn_start_input_tokens = self.total_input_tokens
        _turn_start_output_tokens = self.total_output_tokens
        with trace_span(
            "turn",
            **{
                "langfuse.trace.name": "agent-turn",
                "langfuse.observation.input": user_message[:500],
                "mycode.turn.id": f"{self.session_id}:{self._current_turn}",
                "mycode.turn.number": self._current_turn,
                "mycode.event_range.start_seq": self.session.seq,
                "mycode.sub_agent": self.is_sub_agent,
            },
        ) as turn_span:
            trace_event("turn.start", **trace_kwargs)
            coro = self._chat_openai(user_message)
            self._current_task = asyncio.create_task(coro)
            try:
                await self._current_task
            except asyncio.CancelledError:
                self._aborted = True
                # 确保 user_message 被写入（可能在 _prepare_turn 之前就被 cancel）
                if not self._user_message_written_this_turn:
                    self.session.append("user_message", {"content": original_user_message})
                    self._user_message_written_this_turn = True
                from .observability.trace import trace_error
                trace_error("cancelled", "Turn cancelled", operation="chat")
                turn_span.set_attribute("mycode.aborted", True)
                turn_span.set_attribute("mycode.event_range.end_seq", self.session.seq)
                self.session.append("turn/end", {"turn": self._current_turn, "reason": "aborted", "sub_agent_id": self._current_sub_agent_id})
                raise
            except Exception as e:
                from .observability.trace import trace_error
                trace_error(type(e).__name__, str(e), operation="chat")
                print_error(f"[ERROR] {type(e).__name__}: {e}")
                trace_event(
                    "turn.end",
                    turn=self._turn_number,
                    aborted=True,
                    error=type(e).__name__,
                    duration_s=round(time.time() - _turn_t0, 2),
                )
                turn_span.record_error(e)
                turn_span.set_attribute("mycode.event_range.end_seq", self.session.seq)
                # Send error event to frontend
                self.session.append("error", {"message": str(e), "error_type": type(e).__name__, "sub_agent_id": self._current_sub_agent_id})
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
            turn_span.set_attribute("mycode.event_range.end_seq", self.session.seq)
            turn_span.set_attribute("mycode.aborted", self._aborted)
            turn_span.set_attribute("mycode.tokens.input_delta", self.total_input_tokens - _turn_start_input_tokens)
            turn_span.set_attribute("mycode.tokens.output_delta", self.total_output_tokens - _turn_start_output_tokens)
            turn_span.set_attribute("langfuse.observation.output", assistant_text[:500])
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
            # 使用自定义system prompt时，仍然需要设置session.system_prompt
            self.session.system_prompt = self._custom_system_prompt
            return
        from .core.workspace import set_workspace, reset_workspace
        _ws_token = set_workspace(self.workspace)
        try:
            self._base_system_prompt = build_system_prompt()
            if self.permission_mode == "plan":
                self._system_prompt = self._base_system_prompt + self._build_plan_mode_prompt()
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

    def append_user_message(self, content: str, snapshot_id: str | None = None) -> None:
        """追加用户消息到事件日志。"""
        data = {"content": content}
        if snapshot_id:
            data["snapshot_id"] = snapshot_id
        self.session.append("user_message", data)
        self._user_message_written_this_turn = True

    def append_memory_injection(self, content: str) -> None:
        """追加记忆/Wiki 注入到事件日志（独立事件类型）。

        注入内容（<system-reminder> 包装）是内部提示机制，不是用户消息：
        - LLM 上下文照常包含（get_messages_for_llm 映射为 user role）
        - 前端不渲染为用户气泡，不产生伪 fork/回退点
        """
        self.session.append("memory_injection", {"content": content})

    def append_tool_message(self, tool_call_id: str, content: str, tool_name: str = "") -> None:
        """追加工具结果消息到事件日志。"""
        wrapped = f"<tool_result tool=\"{tool_name}\">\n{content}\n</tool_result>"
        self.session.append("tool_result_msg", {"call_id": tool_call_id, "content": wrapped})

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

    def start_wiki_prefetch(self, user_message: str, side_query) -> None:
        if self.is_sub_agent or self._wiki_prefetch is not None:
            return
        cooled_wiki_paths = {
            path for path, turn in self._wiki_surfaced_at.items()
            if self._turn_number - turn < self.MEMORY_RECALL_COOLDOWN_TURNS
        }
        self._wiki_prefetch_consumed = False
        self._wiki_prefetch = asyncio.create_task(
            select_relevant_wiki_entries(user_message, side_query, cooled_wiki_paths)
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
        )
        self._session_lifecycle.restore(state, data, self.session)
        print_info(f"Session restored ({self._get_message_count()} messages).")

    #/rewind：回退对话 N 轮（纯事件回退；文件恢复走 RevertService 的 stage/commit 流程）。
    def rewind(self, n: int = 1) -> str:
        return self._session_lifecycle.rewind(self.session, n)

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
            start_time=self.session_start_time,
            cwd=str(self.workspace),
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
                start_time=self.session_start_time,
                cwd=str(self.workspace),
            )
            await asyncio.to_thread(self._session_lifecycle._save_state, state, self.session)
        except Exception as e:
            import logging
            logging.getLogger(__name__).error(f"[SAVE] auto-save failed for {self.session_id}: {e}")

    #自动压缩
    async def _check_and_compact(self)->None:
        folded = await self._compressor.run_pipeline(
            self.session,
            self.last_input_token_count,
            self.last_api_call_time,
            self._build_side_query(max_tokens=6000),
            self.session_id,
            self._folded_session_memories,
        )
        if folded:
            self.last_input_token_count = 0

    async def _compact_conversation(self, *, trigger: str = "manual")->bool:
        compacted = await self._compressor.compact_manual(
            self.session,
            self._build_side_query(max_tokens=6000),
            self.session_id,
            self._folded_session_memories,
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
        from .core.session_memory import fallback_folded_memory
        return fallback_folded_memory(transcript)

    async def _record_folded_session_memory(self, trigger: str, memory: dict[str, Any]) -> None:
        import time
        record = {
            "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "trigger": trigger,
            "session_id": self.session_id,
            **memory,
        }
        self._folded_session_memories.append(record)

        if not self.is_sub_agent:
            try:
                from agents.wiki.wiki_capture import capture_session_to_session
                capture_session_to_session(self.session_id, memory)
            except Exception:
                pass

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
        if name in ("read_file", "outline_file", "grep_search", "list_files"):
            return 30
        return 60

    async def _execute_tool_call(self, name: str, inp: dict) -> str:
        from .observability.trace import trace_span
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
        if name == "compact_context":
            return await self._execute_compact_context_tool(inp)
        if name == "context_restore":
            return self._execute_context_restore_tool(inp)
        if name == "search_history":
            return self._execute_search_history_tool(inp)
        if name == "list_task_notes":
            return self._execute_list_task_notes_tool(inp)
        if name == "read_task_notes":
            return self._execute_read_task_notes_tool(inp)
        if name in ("enter_plan_mode", "exit_plan_mode"):
            return await self._execute_plan_mode_tool(name)
        if name == "agent":
            return await self._execute_agent_tool(inp)
        if name == "ask_user":
            from agents.tools.question_tools import handle_ask_user
            return await handle_ask_user(self.session, inp, abort_fn=lambda: self._abort_requested)
        if name == "todolist":
            if self.permission_mode == "plan":
                return "Error: todolist is disabled in plan mode. Use the plan system's tasks.md instead."
            from agents.tools.todo_tools import handle_todolist
            result = handle_todolist(self.session.id, inp)
            self.session.append("todo/updated", {"session_id": self.session.id})
            return result
            # Route MCP tool calls to the MCP manager
        if self._mcp_manager.is_mcp_tool(name):
            return await self._mcp_manager.call_tool(name, inp)
        result = await execute_tool(name, inp, self._read_file_state)
        if name == "skill_create":
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
        """可逆恢复：按 call_id 从事件日志中找到被隐藏的工具结果，返回其原始内容。"""
        key = str(inp.get("key") or "").strip()
        if not key:
            return "Error: 'key' is required. Use the call_id from the tool_folded placeholder."
        
        call_id = key.removeprefix("snip:")
        
        # 收集所有隐藏的 seq
        hidden_seqs = set()
        for event in self.session._log:
            if event.get("type") == "events_hidden":
                hidden_seqs.update(event.get("hidden_seqs", []))
        
        # 找到对应的工具结果事件
        for event in self.session._log:
            if event.get("type") == "tool_result_msg" and event.get("call_id") == call_id:
                if event.get("seq") in hidden_seqs:
                    return event.get("content", "")
        
        # 列出可用的 key
        available = []
        for event in self.session._log:
            if event.get("type") == "tool_result_msg" and event.get("seq") in hidden_seqs:
                available.append(f"snip:{event.get('call_id')}")
        
        hint = f" Available keys: {', '.join(available[:20])}" if available else " No restorable content found."
        return f"Error: no restorable content for key '{key}'.{hint}"

    def _execute_search_history_tool(self, inp: dict) -> str:
        """搜索历史对话（包括被隐藏的）。"""
        query = str(inp.get("query") or "").strip().lower()
        if not query:
            return "Error: 'query' is required."
        
        limit = int(inp.get("limit") or 20)
        
        # 收集所有隐藏的 seq
        hidden_seqs = set()
        for event in self.session._log:
            if event.get("type") == "events_hidden":
                hidden_seqs.update(event.get("hidden_seqs", []))
        
        # 搜索所有消息（包括隐藏的）
        results = []
        for event in self.session._log:
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
                
                # 预览内容
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
        
        # 格式化结果
        result_lines = [f"Found {len(results)} results for '{query}':"]
        for r in results[:limit]:
            hidden_marker = " [HIDDEN]" if r["hidden"] else ""
            result_lines.append(f"  [seq={r['seq']}] {r['type']}{hidden_marker}: {r['content_preview']}")
        
        # 如果有被隐藏的工具结果，提示可恢复
        restorable = [r for r in results if r.get("call_id") and r["hidden"]]
        if restorable:
            result_lines.append("\n--- Restorable tool results ---")
            for r in restorable[:10]:
                result_lines.append(f"  call_id={r['call_id']}: {r['content_preview'][:100]}...")
                result_lines.append(f"  → Use context_restore(key='snip:{r['call_id']}') to restore")
        
        return "\n".join(result_lines)

    def _execute_list_task_notes_tool(self, inp: dict) -> str:
        """列出所有任务笔记，附带最新笔记内容。"""
        limit = int(inp.get("limit") or 10)
        
        # 从 wiki 中读取任务笔记
        from agents.wiki.wiki_manager import get_wiki_dir
        wiki_dir = get_wiki_dir() / "task_notes"
        
        if not wiki_dir.exists():
            return "No task notes found."
        
        # 列出所有任务笔记文件
        notes = []
        for f in wiki_dir.glob("*.md"):
            try:
                content = f.read_text()
                # 解析 frontmatter
                from agents.memory.frontmatter import parse_frontmatter
                meta, body = parse_frontmatter(content)
                
                # 提取 session_id 从文件名
                session_id = f.stem.replace("session_", "")
                
                notes.append({
                    "session_id": session_id,
                    "title": meta.get("name", session_id),
                    "time": meta.get("modified", ""),
                    "content": body,
                })
            except Exception:
                pass
        
        if not notes:
            return "No task notes found."
        
        # 按时间排序，取最新的
        notes.sort(key=lambda x: x["time"], reverse=True)
        notes = notes[:limit]
        
        # 格式化结果
        result_lines = [f"Found {len(notes)} task notes:"]
        for note in notes:
            result_lines.append(f"  [{note['session_id']}] {note['title']} ({note['time']})")
        
        # 附带最新笔记的完整内容
        if notes:
            latest = notes[0]
            result_lines.append(f"\n--- Latest note ({latest['session_id']}) ---")
            result_lines.append(latest['content'])
        
        return "\n".join(result_lines)

    def _execute_read_task_notes_tool(self, inp: dict) -> str:
        """读取指定 session 的任务笔记。"""
        session_id = str(inp.get("session_id") or "").strip()
        
        from agents.wiki.wiki_manager import get_wiki_dir
        wiki_dir = get_wiki_dir() / "task_notes"
        
        if not wiki_dir.exists():
            return "No task notes found."
        
        # 如果指定了 session_id，读取对应的笔记
        if session_id:
            filepath = wiki_dir / f"session_{session_id}.md"
            if not filepath.exists():
                return f"Error: task note for session '{session_id}' not found."
            
            content = filepath.read_text()
            from agents.memory.frontmatter import parse_frontmatter
            meta, body = parse_frontmatter(content)
            return body
        
        # 否则读取当前 session 的笔记
        filepath = wiki_dir / f"session_{self.session_id}.md"
        if not filepath.exists():
            return f"No task notes found for current session '{self.session_id}'."
        
        content = filepath.read_text()
        from agents.memory.frontmatter import parse_frontmatter
        meta, body = parse_frontmatter(content)
        return body


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
        from .observability.trace import trace_span
        
        if name == "enter_plan_mode":
            if self.permission_mode == "plan":
                return "Already in plan mode."
            self._enter_plan_mode_internal()
            return f"Entered plan mode. You are now in read-only mode.\n\nYour plan file: {self._plan_file_path}\nWrite your plan to this file. This is the only file you can edit.\n\nWhen your plan is complete, call exit_plan_mode."
        
        if name == "exit_plan_mode":
            if self.permission_mode != "plan":
                return "Not in plan mode."
            plan_content = "(No plan file found)"
            if self._plan_file_path and Path(self._plan_file_path).exists():
                try:
                    plan_content = Path(self._plan_file_path).read_text()
                except Exception as e:
                    plan_content = f"(Failed to read plan file: {e})"
            
            # 解析三区域（v2.0）
            spec_content = self._extract_plan_section(plan_content, "SPEC")
            plan_section = self._extract_plan_section(plan_content, "PLAN")
            tasks_content = self._extract_plan_section(plan_content, "TASKS")
            
            # 如果没有三区域标记，整体作为 plan 展示（向后兼容）
            if not spec_content and not plan_section and not tasks_content:
                plan_section = plan_content
            
            # 交互式审批流程（如果有审批函数，如 CLI 模式）
            if self._plan_approval_fn:
                result = self._plan_approval_fn(plan_content)
                choice = result.get("choice", "manual-execute")

                if choice == "keep-planning":
                    feedback = result.get("feedback") or "Please revise the plan."
                    
                    # 记录 plan_rejected 到 Langfuse trace
                    with trace_span("plan_mode.rejected",
                        langfuse_observation_type="chain",
                        feedback=feedback[:500] if feedback else "",
                    ):
                        pass
                    
                    return (
                        f"User rejected the plan and wants to keep planning.\n\n"
                        f"User feedback: {feedback}\n\n"
                        f"Please revise your plan based on this feedback. When done, call exit_plan_mode again."
                    )

                if choice in ("clear-and-execute", "execute"):
                    # 自动创建或追加 Plan 系统条目（v2.0）
                    plan_result = self._handle_plan_system_integration(
                        spec_content, plan_section, tasks_content
                    )
                    
                    target_mode = "acceptEdits"
                else:  # manual-execute
                    target_mode = self._pre_plan_mode or "default"
                    plan_result = None

                saved_plan_path = self._plan_file_path
                self.permission_mode = target_mode
                self._pre_plan_mode = None
                self._plan_file_path = None
                self._system_prompt = self._base_system_prompt
                self.session.system_prompt = self._system_prompt

                # 记录 plan_approved 到 Langfuse trace
                with trace_span("plan_mode.approved",
                    langfuse_observation_type="chain",
                    target_mode=target_mode,
                    context_cleared=str(choice == "clear-and-execute"),
                    plan_slug=plan_result.get("slug", "") if plan_result else "",
                ):
                    pass

                if choice == "clear-and-execute":
                    self._clear_history_keep_system()
                    self._context_cleared = True
                    print_info(f"Plan approved. Context cleared, executing in {target_mode} mode.")
                    
                    result_msg = f"User approved the plan. Context was cleared. Permission mode: {target_mode}\n\n"
                    if plan_result:
                        result_msg += f"Plan system entry created: {plan_result.get('slug', '')}\n\n"
                    result_msg += f"Plan file: {saved_plan_path}\n\n"
                    result_msg += f"## Approved Plan:\n{plan_content}\n\n"
                    result_msg += f"Proceed with implementation."
                    return result_msg
                
                print_info(f"Plan approved. Executing in {target_mode} mode.")
                return (
                    f"User approved the plan. Permission mode: {target_mode}\n\n"
                    f"## Approved Plan:\n{plan_content}\n\n"
                    f"Proceed with implementation."
                )
            
            # Web 模式：通过 permission gate 让用户审批计划
            # 将计划内容作为确认消息发送给用户
            confirmed = await self._confirm_dangerous(
                f"Plan completed. Exit plan mode?\n\n## Plan:\n{plan_content}"
            )
            
            if not confirmed:
                # 记录 plan_rejected 到 Langfuse trace
                with trace_span("plan_mode.rejected",
                    langfuse_observation_type="chain",
                    feedback="User rejected the plan.",
                ):
                    pass
                
                return (
                    "User rejected the plan and wants to keep planning.\n\n"
                    "Please revise your plan based on user feedback. When done, call exit_plan_mode again."
                )
            
            # 用户批准，自动创建或追加 Plan 系统条目（v2.0）
            plan_result = self._handle_plan_system_integration(
                spec_content, plan_section, tasks_content
            )
            
            # 退出 plan 模式
            target_mode = self._pre_plan_mode or "default"
            self.permission_mode = target_mode
            self._pre_plan_mode = None
            saved_plan_path = self._plan_file_path
            self._plan_file_path = None
            self._system_prompt = self._base_system_prompt
            self.session.system_prompt = self._system_prompt

            # 记录 plan_approved 到 Langfuse trace
            with trace_span("plan_mode.approved",
                langfuse_observation_type="chain",
                target_mode=target_mode,
                plan_slug=plan_result.get("slug", "") if plan_result else "",
            ):
                pass

            print_info(f"Plan approved. Executing in {target_mode} mode.")
            
            result_msg = f"User approved the plan. Permission mode: {target_mode}\n\n"
            if plan_result:
                result_msg += f"Plan system entry created: {plan_result.get('slug', '')}\n\n"
            result_msg += f"## Approved Plan:\n{plan_content}\n\n"
            result_msg += f"Proceed with implementation."
            return result_msg

        return f"Unknown plan mode tool: {name}"

    def _extract_plan_section(self, content: str, section: str) -> str:
        """从 plan 文件中提取指定区域的内容。"""
        import re
        pattern = rf"<!-- {section} START -->(.*?)<!-- {section} END -->"
        match = re.search(pattern, content, re.DOTALL)
        if match:
            return match.group(1).strip()
        return ""

    def _handle_plan_system_integration(
        self,
        spec_content: str,
        plan_content: str,
        tasks_content: str,
    ) -> dict | None:
        """自动创建或追加 Plan 系统条目（v2.0）。"""
        import re
        from agents.plan.plan_manager import (
            create_plan,
            get_plan,
            append_tasks_to_plan,
            add_artifact,
        )
        from agents.plan.plan_models import PlanGranularity
        
        # 如果没有 tasks，不能创建 plan
        if not tasks_content:
            return None
        
        # 从 spec 提取 slug
        slug = ""
        if spec_content:
            title_match = re.search(r"# Spec:\s*(.+)", spec_content)
            if title_match:
                slug = re.sub(r"[^a-z0-9]+", "-", title_match.group(1).strip().lower()).strip("-")[:40]
        
        if not slug:
            slug = f"plan-{self.session_id}"
        
        # 检查 session 是否已关联 plan
        if self.session.plan_slug:
            # 追加模式
            existing_plan = get_plan(self.session.plan_slug)
            if existing_plan and existing_plan.status.value not in ("archived", "abandoned"):
                try:
                    append_tasks_to_plan(self.session.plan_slug, tasks_content)
                    return {"slug": self.session.plan_slug, "action": "appended"}
                except Exception as e:
                    print_error(f"Failed to append tasks to plan: {e}")
                    return None
        
        # 创建新 plan
        try:
            plan_dir = create_plan(
                slug=slug,
                granularity=PlanGranularity.STANDARD,
            )
            
            # 写入 spec.md
            if spec_content:
                add_artifact(slug, "spec.md", spec_content)
            
            # 写入 design.md
            if plan_content:
                add_artifact(slug, "design.md", plan_content)
            
            # 写入 tasks.md
            add_artifact(slug, "tasks.md", tasks_content)
            
            # 关联 session
            self.session.plan_slug = slug
            
            return {"slug": slug, "action": "created", "plan_dir": str(plan_dir)}
        except Exception as e:
            print_error(f"Failed to create plan: {e}")
            return None

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
        from agents.observability.tracer import tracer
        
        agent_type = inp.get("type", "general")
        description = inp.get("description", "sub-agent task")
        prompt = inp.get("prompt", "")
        print_sub_agent_start(agent_type, description)
        
        import uuid
        sub_agent_id = str(uuid.uuid4())[:8]
        
        with tracer.span("sub_agent.execute", {
            "langfuse.observation.type": "agent",
            "mycode.agent.type": agent_type,
            "mycode.agent.id": sub_agent_id,
            "mycode.agent.description": description[:200],
            "mycode.agent.prompt": prompt[:500],
        }) as span:
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
                duration_s = round(time.time() - start_time, 2)
                self.total_input_tokens += result["tokens"]["input"]
                self.total_output_tokens += result["tokens"]["output"]
                print_sub_agent_end(agent_type, description)
                self.session.append("sub_agent/end", {
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
                self.session.append("sub_agent/end", {
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
