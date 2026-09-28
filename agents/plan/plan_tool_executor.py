"""Plan mode 工具执行（U0 从 Agent._execute_plan_mode_tool 迁出）。

Agent 只留调度：dispatcher 直接调用本模块。行为与原实现逐项等价，
两条审批分支（plan_approval_fn / confirm_dangerous 回退）的重复尾部
收敛为单一流程；已核对的原分支差异全部保留：
- approval_fn 分支 manual-execute：不做 plan system integration（plan_result=None）
- confirm 分支 manual-execute：做 integration 且 slug 存在时照常 start_plan_execution
- trace metadata：confirm 分支原多带 "choice" 字段，统一后两分支都带（仅 trace 元数据）
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from agents.logging import print_info
from agents.observability.trace import trace_event
from agents.plan.plan_executor import PlanExecutor
from agents.plan.plan_manager import start_plan_execution
from agents.plan.plan_mode import PlanModeManager

if TYPE_CHECKING:
    from agents.agent import Agent


def format_plan_tasks_block(exec_result: dict) -> str:
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


async def execute_plan_mode_tool(agent: "Agent", name: str) -> str:
    if name == "enter_plan_mode":
        return _enter_plan_mode(agent)
    if name == "exit_plan_mode":
        return await _exit_plan_mode(agent)
    return f"Unknown plan mode tool: {name}"


def _enter_plan_mode(agent: "Agent") -> str:
    if agent.permission_mode == "plan":
        return "Already in plan mode."
    agent._enter_plan_mode_internal()
    return (
        f"Entered plan mode. You are now in read-only mode.\n\n"
        f"Your plan directory: {agent._plan_mode_manager.plan_dir}\n"
        f"双轨规划（二选一）：\n"
        f"- 轻量轨（默认）：只写 plan.md（## 背景 / ## 方案 / ## 任务清单（checkbox：- [ ] 1. 描述）/ ## 验收）\n"
        f"- 重量轨（复杂任务）：写 spec.md（含验收标准）+ design.md + tasks.md\n\n"
        f"When your plan is complete, call exit_plan_mode."
    )


@dataclass
class _PlanDraft:
    """exit_plan_mode 的产物读取结果（轻量轨/重量轨自动判定后）。"""
    granularity: str
    spec: str
    design: str
    tasks: str
    plan_md: str
    full_plan: str


def _validate_and_load_draft(mgr: Any) -> str | _PlanDraft:
    """校验产物完整性并读取草稿；返回 str 时为提前返回的失败消息。"""
    validation = mgr.validate_plan_artifacts()
    if not validation["valid"]:
        errors = "\n".join(f"- {e}" for e in validation["errors"])
        return f"Plan artifacts validation failed:\n{errors}\n\nPlease complete your plan before exiting."

    # 读取产物内容（自动判定轻量轨/重量轨）
    draft = mgr.read_draft_artifacts()
    granularity = validation.get("granularity", draft["granularity"])
    spec_content = draft["spec"]
    plan_content = draft["design"]
    tasks_content = draft["tasks"]
    plan_md = draft["plan"]
    if granularity == "minimal" and not tasks_content:
        tasks_content = PlanModeManager.checkbox_tasks_to_structured(plan_md)

    full_plan = "\n\n".join(
        part for part in (
            plan_md.strip() if granularity == "minimal" and plan_md.strip() else "",
            f"## Spec\n{spec_content}" if spec_content.strip() else "",
            f"## Design\n{plan_content}" if plan_content.strip() else "",
            f"## Tasks\n{tasks_content}" if granularity != "minimal" and tasks_content.strip() else "",
        ) if part
    ) or "(empty plan)"
    return _PlanDraft(granularity, spec_content, plan_content, tasks_content, plan_md, full_plan)


async def _request_approval(agent: "Agent", mgr: Any, full_plan: str) -> tuple[str, str | None]:
    """审批双通道：plan_approval_fn（前端交互）优先，confirm_dangerous（CLI）回退。

    返回 (choice, feedback)。
    """
    if mgr.plan_approval_fn is not None:
        result = await mgr.plan_approval_fn(full_plan)
        return result.get("choice", "manual-execute"), result.get("feedback")

    confirmed = await agent._confirm_dangerous(
        f"Plan completed. Exit plan mode?\n\n{full_plan}",
        extra_data={"plan_dir": str(mgr.plan_dir)} if mgr.plan_dir else None,
        tool_name="exit_plan_mode",
    )
    if confirmed:
        return agent._permission_gate.last_choice or "execute", None
    return "keep-planning", agent._permission_gate.last_feedback or "User rejected the plan without comments."


async def _exit_plan_mode(agent: "Agent") -> str:

    if agent.permission_mode != "plan":
        return "Not in plan mode."

    mgr = agent._plan_mode_manager
    draft = _validate_and_load_draft(mgr)
    if isinstance(draft, str):
        return draft

    choice, feedback = await _request_approval(agent, mgr, draft.full_plan)

    if choice == "keep-planning":
        feedback = feedback or "Please revise the plan."
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

    return _finalize_plan_exit(agent, mgr, draft, choice)


def _finalize_plan_exit(agent: "Agent", mgr: Any, draft: _PlanDraft, choice: str) -> str:
    """批准路径：integration 落地 + 模式切换 + trace 上报 + 结果消息组装。"""

    execute = choice in ("clear-and-execute", "execute")
    # 行为保留：approval_fn 分支仅 execute/clear-and-execute 做 integration；
    # confirm 分支（含 manual-execute）始终做 integration。
    if execute or mgr.plan_approval_fn is None:
        plan_result: dict[str, Any] | None = mgr.handle_plan_system_integration(
            draft.spec, draft.design, draft.tasks, agent.session,
            granularity=draft.granularity, plan_md=draft.plan_md,
        )
    else:
        plan_result = None
    target_mode = "acceptEdits" if execute else (mgr.pre_plan_mode or "default")

    saved_plan_dir = mgr.plan_dir
    agent.permission_mode = target_mode
    mgr.pre_plan_mode = None
    mgr.plan_dir = None
    agent._system_prompt = agent._base_system_prompt
    agent.session.system_prompt = agent._system_prompt
    agent._emit_permission_mode_event()

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
        agent._context_manager.clear_history_keep_system()
        agent._context_cleared = True
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
    result_msg += f"## Approved Plan:\n{draft.full_plan}\n\n"

    if plan_result and plan_result.get("slug"):
        plan_slug = plan_result["slug"]
        print_info(f"Starting plan execution: {plan_slug}")

        exec_result = start_plan_execution(plan_slug)
        result_msg += format_plan_tasks_block(exec_result)
    else:
        result_msg += "Proceed with implementation."

    return result_msg
