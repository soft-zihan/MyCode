"""子代理生命周期（spawn / 续跑 / job 运行 / 结果封装 / 事件与 trace 上报 / 后台完成通知）。

U0 第 5 项：从 dispatcher._execute_agent_tool（197 行）+ Agent._spawn_sub_agent 迁出。
U2：可续跑子代理（事件流恢复 / 归属校验 / steer join / <subagent> 标签协议）。
U3a：后台子代理——job_registry 承载 in-flight 句柄（done/backgrounded 双事件），
前台阻塞 = race(done, backgrounded) 二相等待（v2 job.ts:330-357），后台化完成后经
统一数据流向父会话注入 subagent/completed synthetic 事件（v2 subagent-completion.ts:20-45）。
dispatcher 经函数内延迟 import 调用本模块（避免 agent→dispatcher→runner→agent 环）。
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import TYPE_CHECKING, Any

from agents.core.job_registry import SubAgentJob, get_job, pop_job, register_job
from agents.core.session import Session
from agents.tools.result import ToolExecutionResult

if TYPE_CHECKING:
    from agents.agent import Agent


def spawn_sub_agent(
    parent: "Agent",
    *,
    system_prompt: str,
    tools: list,
    model_ref: str,
    label: str,
    max_tool_calls: int | None = None,
) -> "Agent":
    """按父代理上下文派生子代理（endpoint 路由 / plan 模式继承 / abort 联动）。

    BC-20 修复：compression_arm/context_window 随 spawn 透传——子代理与父走同一
    压缩实验臂与窗口，LOCA 类消融实验不再被子代理逃逸。
    """
    from agents.agent import Agent
    from agents.core.model_registry import resolve_agent_endpoint

    endpoint = resolve_agent_endpoint(label, model_ref=model_ref, primary=parent._primary_endpoint())
    return Agent(
        model=endpoint.model,
        api_base=endpoint.base_url,
        api_key=endpoint.api_key,
        custom_system_prompt=system_prompt,
        custom_tools=tools,
        is_sub_agent=True,
        max_tool_calls=max_tool_calls,
        permission_mode="plan" if parent.permission_mode == "plan" else "bypassPermissions",
        parent_abort_event=parent._abort_event,
        workspace=parent.workspace,
        compression_arm=parent.compression_arm,
        context_window=parent.context_window,
    )


# 内层超时余量：runner 先于 dispatcher 外层 wait_for 超时，才能返回带
# <subagent session_id state> 标签的 timeout 结果（外层只作兜底，文本无标签）
_INNER_TIMEOUT_MARGIN_S = 5

# U3a 反轮询文案（诚实版，参数描述+工具结果双处——v2 shell.ts:25-26 模式）：
# U3a 无自动唤醒，不抄 v2 "you will be resumed automatically" 承诺（见方案 U3 路线复审）。
_BACKGROUND_GUIDANCE = (
    "When it finishes, its result is automatically injected into this session "
    "(as a <subagent ...> message) and rendered in the frontend; you will see it "
    "at the start of your next turn. Do NOT sleep, poll, or spawn a duplicate "
    "sub-agent for the same work. If you have nothing to do that does not depend "
    "on it, end your reply."
)


def _inner_timeout(timeout_s: int) -> int:
    if timeout_s and timeout_s > _INNER_TIMEOUT_MARGIN_S * 2:
        return timeout_s - _INNER_TIMEOUT_MARGIN_S
    return timeout_s


def _tag_subagent(text: str, sub_session_id: str, state_label: str) -> str:
    """v2 subagent.ts:245-265：结果统一带 <subagent sessionID state> 标签，
    失败/取消/超时同样带 ID——模型据此续跑，评测据此锚定。"""
    return f'<subagent session_id="{sub_session_id}" state="{state_label}">{text}</subagent>'


def _state_label(status: str) -> str:
    return "cancelled" if status in ("cancelled", "aborted") else status


async def execute_agent_tool(agent: "Agent", inp: dict, *, timeout_s: int) -> str | ToolExecutionResult:
    """执行 agent 工具（子 Agent）。session_id → 续跑；background → 发起后立即返回。"""
    resume_id = str(inp.get("session_id") or "").strip()
    if resume_id:
        return await _resume_agent_tool(agent, inp, resume_id, timeout_s=timeout_s)

    from agents.core.subagent import get_sub_agent_config
    from agents.logging import print_sub_agent_start

    agent_type = inp.get("type", "general")
    description = inp.get("description", "sub-agent task")
    prompt = inp.get("prompt", "")
    config = get_sub_agent_config(agent_type)
    max_tool_calls = config.get("max_tool_calls")
    print_sub_agent_start(agent_type, description)

    sub_agent_id = str(uuid.uuid4())[:8]
    sub_session, sub_agent = _start_sub_agent(agent, agent_type, config, sub_agent_id, max_tool_calls, timeout_s, description)
    return await _launch_job(
        agent, sub_agent, sub_session, prompt,
        agent_type=agent_type, description=description,
        timeout_s=timeout_s, max_tool_calls=max_tool_calls,
        background=bool(inp.get("background")),
    )


async def _launch_job(agent: "Agent", sub_agent: "Agent", sub_session: Session, prompt: str,
                      *, agent_type: str, description: str, timeout_s: int,
                      max_tool_calls: int | None, background: bool, resume: bool = False) -> ToolExecutionResult:
    """注册 job 并启动全生命周期 task（spawn/续跑共用）。

    background=true：标记后台化并立即返回 running 结果；
    否则前台阻塞 = race(done, backgrounded) 二相等待，可被中途转后台。
    """
    job = SubAgentJob(
        sub_session_id=sub_session.id,
        parent_session_id=agent.session_id,
        agent_type=agent_type,
        description=description,
        sub_agent=sub_agent,
    )
    job.task = asyncio.create_task(_run_sub_agent_job(
        agent, job, sub_agent, sub_session, prompt,
        timeout_s=timeout_s, max_tool_calls=max_tool_calls, resume=resume,
    ))
    register_job(job)

    if background:
        job.mark_background()
        return ToolExecutionResult(
            text=_tag_subagent(
                f"Sub-agent launched in background. {_BACKGROUND_GUIDANCE}",
                sub_session.id, "running",
            ),
            status="ok",
            outcome="success",
            metadata={"status": "running", "sub_session_id": sub_session.id, "background": True},
        )
    return await _block_on_job(job)


async def _block_on_job(job: SubAgentJob) -> ToolExecutionResult:
    """前台二相等待：race(done, backgrounded)（v2 job.ts:330-357）。

    外层硬取消（abort/dispatcher 超时兜底）且未后台化 → 级联杀 run（保持旧语义）；
    已后台化 → run 继续，结果由 subagent/completed 通知负责送达。
    """
    done_wait = asyncio.create_task(job.done.wait())
    bg_wait = asyncio.create_task(job.backgrounded.wait())
    try:
        await asyncio.wait({done_wait, bg_wait}, return_when=asyncio.FIRST_COMPLETED)
    except asyncio.CancelledError:
        if not job.backgrounded.is_set() and job.task is not None and not job.task.done():
            job.task.cancel()
        raise
    finally:
        for t in (done_wait, bg_wait):
            if not t.done():
                t.cancel()

    if job.backgrounded.is_set():
        return ToolExecutionResult(
            text=_tag_subagent(
                f"Sub-agent moved to background and keeps running. {_BACKGROUND_GUIDANCE}",
                job.sub_session_id, "backgrounded",
            ),
            status="ok",
            outcome="success",
            metadata={"status": "backgrounded", "sub_session_id": job.sub_session_id, "background": True},
        )
    if job.result is not None:
        return job.result
    status = job.state.get("status") or "error"
    return ToolExecutionResult(
        text=_tag_subagent(
            job.state.get("output_text") or f"Sub-agent {status}",
            job.sub_session_id, _state_label(status),
        ),
        status="error",
        outcome=job.state.get("outcome") or "error",
        metadata={"reason": job.state.get("summary") or status, "sub_session_id": job.sub_session_id},
    )


async def _run_sub_agent_job(agent: "Agent", job: SubAgentJob, sub_agent: "Agent",
                             sub_session: Session, prompt: str, *, timeout_s: int,
                             max_tool_calls: int | None, resume: bool = False) -> None:
    """全生命周期 task：trace span → run → 封装 → sub_agent/end → 完成通知 → 注册表清理。

    在独立 task 中运行：前台提前返回（后台发起/转后台）后 span 与事件仍完整落账
    （v2 subagent-job.ts:36-53 的 run=resume+取最后 assistant 文本模式）。
    """
    from agents.observability.trace import trace_span

    state: dict[str, Any] = {
        "status": "error", "outcome": "error", "summary": "", "output_text": "",
        "input_tokens": 0, "output_tokens": 0, "stop_reason": None, "error": None,
    }
    start_time = time.time()
    try:
        with trace_span(
            "sub_agent",
            name=f"agent.{job.agent_type}",
            input=prompt[:4000],
            metadata={
                "agent_id": job.sub_session_id,
                "agent_type": job.agent_type,
                "description": job.description[:500],
                "parent_session_id": job.parent_session_id,
                "timeout_s": timeout_s,
                "max_tool_calls": max_tool_calls,
                "resume": resume,
                "sub_session_id": job.sub_session_id,
            },
        ) as span:
            run_task = asyncio.create_task(sub_agent.run_once(prompt))
            try:
                job.result = await _await_sub_agent_run(
                    agent, sub_agent, sub_session, run_task, state,
                    timeout_s=timeout_s, max_tool_calls=max_tool_calls,
                )
            finally:
                _finalize_sub_agent(
                    agent, span, sub_agent, sub_session, job.sub_session_id,
                    job.agent_type, job.description, state, start_time, timeout_s, max_tool_calls,
                )
    finally:
        job.state = state
        _fire_completion_notification(agent, job)
        pop_job(job.sub_session_id)
        job.done.set()


def _fire_completion_notification(agent: "Agent", job: SubAgentJob) -> None:
    """U3a：后台化 job 结束 → 父会话注入 subagent/completed synthetic 事件。

    走统一数据流（v2 subagent-completion.ts:20-45）：一次 append 同时完成
    持久化 + 投影（derive 为 user 消息，下一轮模型自动可见）+ WS 广播（前端完成卡片）。
    notification_id 预分配幂等——done 与 cancel 竞态可能双触发（v2 job.ts:38,199-209,366）。
    """
    if not job.backgrounded.is_set():
        return
    parent = agent.session
    if any(
        e.get("type") == "subagent/completed" and e.get("notification_id") == job.notification_id
        for e in parent.events
    ):
        return
    state = job.state
    status = state.get("status") or "error"
    if job.result is not None:
        text = job.result.text
    else:
        text = _tag_subagent(
            state.get("output_text") or f"Sub-agent {status}",
            job.sub_session_id, _state_label(status),
        )
    parent.append("subagent/completed", {
        "notification_id": job.notification_id,
        "sub_session_id": job.sub_session_id,
        "agent_type": job.agent_type,
        "description": job.description,
        "status": status,
        "outcome": state.get("outcome"),
        "summary": (state.get("summary") or "")[:500],
        "text": text,
        "input_tokens": state.get("input_tokens", 0),
        "output_tokens": state.get("output_tokens", 0),
    })


async def _resume_agent_tool(agent: "Agent", inp: dict, resume_id: str, *, timeout_s: int) -> str | ToolExecutionResult:
    """U2 续跑子会话：归属校验 → 运行中 steer+join / 空闲则从事件流恢复起新 turn。"""
    owned = any(
        ev.get("type") == "sub_agent/start" and ev.get("sub_session_id") == resume_id
        for ev in agent.session.events
    )
    if not owned:
        return ToolExecutionResult(
            text=(
                f"Error: session_id '{resume_id}' is not a sub-agent of this session. "
                "Only sub-agents you spawned (see their <subagent session_id=...> result tag) can be resumed."
            ),
            status="error",
            outcome="error",
            metadata={"reason": "resume_not_owned", "sub_session_id": resume_id},
        )

    job = get_job(resume_id)
    if job is not None and not job.done.is_set():
        return await _join_running_sub_agent(agent, inp, resume_id, job, timeout_s=timeout_s)
    return await _resume_idle_sub_agent(agent, inp, resume_id, timeout_s=timeout_s)


async def _join_running_sub_agent(agent: "Agent", inp: dict, resume_id: str,
                                  job: SubAgentJob, *, timeout_s: int) -> ToolExecutionResult:
    """子会话仍在运行：prompt 经 steer 队列注入（U1 通道），等待 job done（join 去重）。

    等待的是 done 事件而非 run task 本体：join 方自身超时/取消不连带杀死原 run。
    token 记账归属 run 生命周期 task，join 方不再重复累加（防双计）。
    """
    prompt = inp.get("prompt", "")
    description = inp.get("description", "resume (join running)")
    await job.sub_agent.message_queue.steer(prompt, source="user")
    agent.session.append("sub_agent/resume", {
        "sub_session_id": resume_id,
        "mode": "join",
        "description": description,
    })
    try:
        await asyncio.wait_for(job.done.wait(), timeout=_inner_timeout(timeout_s))
    except asyncio.TimeoutError:
        return ToolExecutionResult(
            text=_tag_subagent(
                f"Joined sub-agent is still running; no result within {timeout_s}s. "
                "It was NOT cancelled — resume again later with the same session_id.",
                resume_id, "timeout",
            ),
            status="error",
            outcome="timeout",
            metadata={"reason": "join_timeout", "sub_session_id": resume_id, "partial": True},
        )

    if job.result is not None:
        base = job.result
        return ToolExecutionResult(
            text=base.text,
            status=base.status,
            outcome=base.outcome,
            metadata={**base.metadata, "reason": "joined"},
        )
    status = job.state.get("status") or "error"
    return ToolExecutionResult(
        text=_tag_subagent(
            job.state.get("output_text") or f"Sub-agent {status}",
            resume_id, _state_label(status),
        ),
        status="error",
        outcome=job.state.get("outcome") or "error",
        metadata={"reason": "joined", "sub_session_id": resume_id},
    )


async def _resume_idle_sub_agent(agent: "Agent", inp: dict, resume_id: str, *, timeout_s: int) -> ToolExecutionResult:
    """空闲子会话：从事件流恢复 Session（含 crash recovery），配置现场重解析后起新 turn。"""
    from agents.core.subagent import get_sub_agent_config
    from agents.logging import print_sub_agent_start

    sub_session = Session.load_from_events(resume_id)
    if sub_session is None:
        return ToolExecutionResult(
            text=f"Error: sub-session '{resume_id}' has no persisted events; cannot resume.",
            status="error",
            outcome="error",
            metadata={"reason": "resume_not_found", "sub_session_id": resume_id},
        )

    # 续跑可换 agent/model（v2 subagent.ts:168-182）；配置不快照，每次现场重解析
    agent_type = str(inp.get("type") or "").strip() or sub_session.agent_type or "general"
    config = get_sub_agent_config(agent_type)
    max_tool_calls = config.get("max_tool_calls")
    description = inp.get("description", f"resume {agent_type}")
    prompt = inp.get("prompt", "")
    print_sub_agent_start(agent_type, f"[resume] {description}")

    sub_agent = agent._spawn_sub_agent(
        system_prompt=config["system_prompt"],
        tools=config["tools"],
        model_ref=config.get("model_ref", ""),
        label=agent_type,
        max_tool_calls=max_tool_calls,
    )
    sub_agent.session = sub_session
    sub_agent.session_id = sub_session.id
    sub_agent._current_sub_agent_id = resume_id

    agent.session.append("sub_agent/resume", {
        "sub_session_id": resume_id,
        "mode": "turn",
        "agent_type": agent_type,
        "description": description,
        "timeout_s": timeout_s,
        "max_tool_calls": max_tool_calls,
        "background": bool(inp.get("background")),
    })

    return await _launch_job(
        agent, sub_agent, sub_session, prompt,
        agent_type=agent_type, description=description,
        timeout_s=timeout_s, max_tool_calls=max_tool_calls,
        background=bool(inp.get("background")), resume=True,
    )


async def _await_sub_agent_run(agent: "Agent", sub_agent: "Agent", sub_session: Session,
                               run_task: "asyncio.Task[dict]", state: dict[str, Any], *,
                               timeout_s: int, max_tool_calls: int | None) -> ToolExecutionResult:
    """等待子代理 run 并封装结果（spawn/resume 共用）。

    超时语义：runner 内层 wait_for（余量 5s）先到期 → 返回带标签的 timeout 结果；
    dispatcher 外层 wait_for 只作兜底。父级硬中止（CancelledError）继续上抛，
    由 job 生命周期 finally 统一收尾（sub_agent/end + 完成通知 + done）。
    """
    try:
        result = await asyncio.wait_for(run_task, timeout=_inner_timeout(timeout_s))
    except asyncio.TimeoutError:
        state["status"] = "timeout"
        state["outcome"] = "timeout"
        state["summary"] = f"Sub-agent timed out after {timeout_s}s"
        state["error"] = TimeoutError(state["summary"])
        state["output_text"] = state["summary"]
        return ToolExecutionResult(
            text=_tag_subagent(state["summary"], sub_session.id, "timeout"),
            status="error",
            outcome="timeout",
            metadata={"reason": state["summary"], "sub_session_id": sub_session.id, "partial": True},
        )
    except asyncio.CancelledError:
        if not run_task.done():
            run_task.cancel()
        if agent.abort_requested():
            state["status"] = "cancelled"
            state["outcome"] = "cancelled"
            state["summary"] = "Sub-agent cancelled by parent abort"
        else:
            state["status"] = "timeout"
            state["outcome"] = "timeout"
            state["summary"] = f"Sub-agent timed out after {timeout_s}s"
            state["error"] = TimeoutError(state["summary"])
        state["output_text"] = state["summary"]
        raise
    except Exception as e:
        state["status"] = "error"
        state["outcome"] = "error"
        state["summary"] = f"{type(e).__name__}: {e}"
        state["output_text"] = f"Sub-agent error: {e}"
        state["error"] = e
        return ToolExecutionResult(
            text=_tag_subagent(state["output_text"], sub_session.id, "error"),
            status="error",
            outcome="error",
            metadata={"reason": state["summary"], "sub_session_id": sub_session.id},
        )

    state["input_tokens"] = int(result.get("tokens", {}).get("input", 0))
    state["output_tokens"] = int(result.get("tokens", {}).get("output", 0))
    agent.total_input_tokens += state["input_tokens"]
    agent.total_output_tokens += state["output_tokens"]
    state["output_text"] = result.get("text") or "(Sub-agent produced no output)"
    state["summary"] = state["output_text"][:500]
    state["stop_reason"] = result.get("stop_reason")
    state["status"], state["outcome"], state["summary"], tool_result = _encapsulate_result(
        result, sub_agent, sub_session, max_tool_calls,
        state["output_text"], state["summary"], state["stop_reason"],
    )
    return tool_result


def _start_sub_agent(agent: "Agent", agent_type: str, config: dict, sub_agent_id: str,
                     max_tool_calls: int | None, timeout_s: int, description: str):
    """创建子会话 + 派生子代理 + 落 sub_agent/start 事件。"""
    sub_session = Session(
        session_id=sub_agent_id,
        parent_session=agent.session_id,
        origin="sub_agent",
        agent_type=agent_type,
    )
    # U2：子会话首事件落归属元数据（持久化+投影），会话列表过滤与续跑归属校验的唯一数据源
    sub_session.append("session/meta", {
        "origin": "sub_agent",
        "parent_session": agent.session_id,
        "agent_type": agent_type,
    })

    agent.session.append("sub_agent/start", {
        "agent_id": sub_agent_id,
        "agent_type": agent_type,
        "description": description,
        "sub_session_id": sub_session.id,
        "timeout_s": timeout_s,
        "max_tool_calls": max_tool_calls,
    })

    sub_agent = agent._spawn_sub_agent(
        system_prompt=config["system_prompt"],
        tools=config["tools"],
        model_ref=config.get("model_ref", ""),
        label=agent_type,
        max_tool_calls=max_tool_calls,
    )

    sub_agent.session = sub_session
    sub_agent.session_id = sub_session.id
    sub_agent._current_sub_agent_id = sub_agent_id
    return sub_session, sub_agent


def _sub_agent_counts(sub_agent: "Agent") -> dict[str, int]:
    return {
        "tool_call_count": int(getattr(sub_agent, "_tool_call_count", 0) or 0),
        "failed_tool_call_count": int(getattr(sub_agent, "_failed_tool_call_count", 0) or 0),
    }


def _encapsulate_result(result: dict, sub_agent: "Agent", sub_session: Session,
                        max_tool_calls: int | None, output_text: str, summary: str,
                        stop_reason: str | None) -> tuple[str, str, str, ToolExecutionResult]:
    """运行结果 → (status, outcome, summary, ToolExecutionResult)，四路径语义与原实现一致。

    U2：所有路径 text 统一带 <subagent session_id state> 标签（含 aborted/budget/blocked）。
    """
    if sub_agent._aborted:
        return "aborted", "cancelled", summary, ToolExecutionResult(
            text=_tag_subagent("(Sub-agent aborted)", sub_session.id, "cancelled"),
            status="cancelled",
            outcome="cancelled",
            metadata={"reason": "aborted", "sub_session_id": sub_session.id},
        )
    if result.get("tool_budget_exceeded"):
        summary = (
            f"Sub-agent stopped after tool budget: "
            f"{result.get('tool_call_count', 0)}/{max_tool_calls}. {summary}"
        ).strip()
        return "budget_exceeded", "budget_exceeded", summary, ToolExecutionResult(
            text=_tag_subagent(output_text, sub_session.id, "budget_exceeded"),
            status="ok",
            outcome="budget_exceeded",
            metadata={
                "reason": "tool_budget_exceeded",
                **_sub_agent_counts(sub_agent),
                "max_tool_calls": max_tool_calls,
                "partial": True,
                "sub_session_id": sub_session.id,
            },
        )
    if stop_reason:
        summary = f"Sub-agent stopped by {stop_reason}. {summary}".strip()
        return "completed", "blocked", summary, ToolExecutionResult(
            text=_tag_subagent(output_text, sub_session.id, "blocked"),
            status="ok",
            outcome="blocked",
            metadata={
                "reason": stop_reason,
                **_sub_agent_counts(sub_agent),
                "max_tool_calls": max_tool_calls,
                "partial": True,
                "sub_session_id": sub_session.id,
            },
        )
    return "completed", "success", summary, ToolExecutionResult(
        text=_tag_subagent(output_text, sub_session.id, "completed"),
        status="ok",
        outcome="success",
        metadata={
            **_sub_agent_counts(sub_agent),
            "max_tool_calls": max_tool_calls,
            "sub_session_id": sub_session.id,
        },
    )


def _finalize_sub_agent(agent: "Agent", span, sub_agent: "Agent", sub_session: Session,
                        sub_agent_id: str, agent_type: str, description: str,
                        state: dict[str, Any], start_time: float, timeout_s: int,
                        max_tool_calls: int | None) -> None:
    """落 sub_agent/end 事件 + span 收尾（无论成功/取消/异常都执行）。"""
    from agents.logging import print_sub_agent_end

    duration_s = round(time.time() - start_time, 2)
    tool_call_count = int(getattr(sub_agent, "_tool_call_count", 0) or 0)
    failed_tool_call_count = int(getattr(sub_agent, "_failed_tool_call_count", 0) or 0)
    child_turn_count = max(int(getattr(sub_agent, "_turn_number", 0) or 0), 1 if tool_call_count else 0)
    print_sub_agent_end(agent_type, description)
    agent.session.append("sub_agent/end", {
        "agent_id": sub_agent_id,
        "status": state["status"],
        "outcome": state["outcome"],
        "summary": state["summary"][:500],
        "duration_ms": int(duration_s * 1000),
        "sub_session_id": sub_session.id,
        "timeout_s": timeout_s,
        "max_tool_calls": max_tool_calls,
        "stop_reason": state["stop_reason"],
        "tool_call_count": tool_call_count,
        "failed_tool_call_count": failed_tool_call_count,
        "child_turn_count": child_turn_count,
    })
    span_metadata = {
        "status": state["status"],
        "outcome": state["outcome"],
        "duration_s": duration_s,
        "input_tokens": state["input_tokens"],
        "output_tokens": state["output_tokens"],
        "summary": state["summary"][:500],
        "sub_session_id": sub_session.id,
        "timeout_s": timeout_s,
        "max_tool_calls": max_tool_calls,
        "stop_reason": state["stop_reason"],
        "tool_call_count": tool_call_count,
        "failed_tool_call_count": failed_tool_call_count,
        "child_turn_count": child_turn_count,
        "backgrounded": bool(get_job(sub_session.id) and get_job(sub_session.id).backgrounded.is_set()),
    }
    span.update(output=state["output_text"][:20000], metadata=span_metadata)
    if state["error"] is not None:
        span.record_error(state["error"])
