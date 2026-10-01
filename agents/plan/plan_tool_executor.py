"""Plan mode 工具执行（U0 从 Agent._execute_plan_mode_tool 迁出）。

Agent 只留调度：dispatcher 直接调用本模块。行为与原实现逐项等价，
两条审批分支（plan_approval_fn / confirm_dangerous 回退）的重复尾部
收敛为单一流程；已核对的原分支差异全部保留：
- approval_fn 分支 manual-execute：不做 plan system integration（plan_result=None）
- confirm 分支 manual-execute：做 integration 且 slug 存在时照常物化进 task_list
- trace metadata：confirm 分支原多带 "choice" 字段，统一后两分支都带（仅 trace 元数据）

批准后不再启动 plan 执行状态机、也不再向对话注入全量任务清单（两者的注入点
Plan 3a Task 3 已删）：tasks.md 解析出的任务物化进 task_list
（_materialize_plan_into_task_list），清单摘要 S 常驻上下文尾部，焦点任务的
detail 走条件披露注入。tasks.md 在批准那一刻冻结为「已批准的计划」文档，
此后活状态只在 task_list。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from agents.logging import print_error, print_info
from agents.observability.trace import trace_event
from agents.plan.plan_manager import get_tasks
from agents.plan.plan_mode import PlanModeManager
from agents.tools.task_store import add_task, update_task

if TYPE_CHECKING:
    from agents.agent import Agent


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
        materialized = _materialize_plan_into_task_list(agent.session.id, plan_slug)
        print_info(f"Plan approved: materialized {materialized} tasks into task_list ({plan_slug})")
        if materialized:
            result_msg += (
                f"{materialized} 条任务已物化进 task_list。\n"
                "清单摘要常驻你的上下文尾部，无需调 list 查询进度。\n"
                "第 1 条的详细执行方案会在下一次模型调用时自动注入。\n"
                f"已批准的计划文档冻结在 {saved_plan_dir}，此后活状态只在 task_list。"
            )
        else:
            result_msg += "Proceed with implementation."
    else:
        result_msg += "Proceed with implementation."

    return result_msg


# ── plan → task_list 物化（Plan 3a Task 3，spec §九）──

def _compose_detail(task: Any) -> str:
    """把 Task 的结构化字段与任务块正文组合成散文 detail。

    acceptance 不进来——它是单独字段，重复一遍只会浪费常驻与注入预算。
    """
    parts: list[str] = []
    if getattr(task, "file", ""):
        parts.append(f"涉及文件: {task.file}")
    if getattr(task, "function", ""):
        parts.append(f"涉及函数: {task.function}")
    if getattr(task, "interface", ""):
        parts.append(f"接口: {task.interface}")
    body = (getattr(task, "body", "") or "").strip()
    if body:
        parts.append(body)
    return "\n".join(parts)


_PLAN_TO_TASK_STATUS = {
    "pending": "pending",
    "in-progress": "in_progress",
    "done": "completed",
    "skipped": "skipped",
    "failed": "failed",
}


def _materialize_plan_into_task_list(session_id: str, slug: str) -> int:
    """把已批准 plan 的任务物化进 task_list。返回物化条数。

    刻意不传 current_seq：detail 来自磁盘上的 tasks.md，从不在模型自己的
    tool_calls 里，所以 detail_origin_seq 必须保持 None，好让首条方案在批准后
    第一次模型调用时被 ensure_focus_detail_visible 注入。传了 current_seq 反而
    会让它被判为「已在上下文里」而永不注入。

    追加而不清空：session 可能已有清单（add_task 默认追加，after_id 显式串起
    物化顺序与 tasks.md 一致）。物化失败不能让已批准的 plan 中止——用户已经
    批过了，_finalize_plan_exit 必须走完：解析失败退化为「物化 0 条，照常进入
    实现」，中途 store 写失败退化为已成功的前缀条数，都留痕。
    """
    count = 0
    previous_id: int | None = None
    try:
        for task in get_tasks(slug):
            status = _PLAN_TO_TASK_STATUS.get(getattr(task, "status", "pending"), "pending")
            item = add_task(
                session_id,
                content=getattr(task, "description", "") or f"Task {getattr(task, 'id', count + 1)}",
                detail=_compose_detail(task),
                acceptance=getattr(task, "acceptance", "") or "",
                after_id=previous_id,
            )
            previous_id = item.id
            if status != "pending":
                update_task(session_id, item.id, status=status,
                            error=getattr(task, "error", "") or "")
            count += 1
    except Exception as e:
        print_error(f"[plan] materialize failed for '{slug}' after {count} task(s): {e!r}")
    return count
