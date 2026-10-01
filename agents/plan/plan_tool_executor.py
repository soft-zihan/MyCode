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

同理，批准结果消息里嵌的是 conversation_plan（不含任务清单）而不是 full_plan：
清单已经物化，S 又每请求常驻，再抄一遍就是同一个重复账。full_plan 剩下的两个
消费者都是给人看的（前端批准弹窗、CLI 确认框），那边一个字不减。

同一 session 二次批准会走 handle_plan_system_integration 的 append 分支，盘上的
tasks.md 于是成为「旧 + 新」的合并体；那条路径只物化 draft.tasks（本次新增的
一块），否则 task_list 会整份翻倍。详见 _finalize_plan_exit。同一笔重复账还有上游
的一半：草稿目录 plan-<session_id> 每轮同路径，批准落地后必须清掉，否则下一轮的
plan.md 里还躺着上一轮的 checkbox、被转换器整个重转一遍。详见 _clear_draft_dir。
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agents.logging import print_error, print_info
from agents.observability.trace import trace_event
from agents.plan.plan_manager import get_plans_dir, get_tasks, parse_tasks_content
from agents.plan.plan_mode import PlanModeManager
from agents.tools.task_store import add_task, find_focus, list_tasks, update_task

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
    """exit_plan_mode 的产物读取结果（轻量轨/重量轨自动判定后）。

    full_plan 与 conversation_plan 同源但**不同用途**，不可互换：
    - `full_plan` 给人看——plan_approval_fn 的前端批准弹窗、_confirm_dangerous 的
      CLI 确认框。人正是要审这份任务清单，一个字不能少。
    - `conversation_plan` 进对话（批准结果消息）。任务已经物化进 task_list、
      摘要 S 常驻上下文尾部，再把清单抄一遍就是 format_plan_tasks_block
      （本任务已删）那笔重复账，所以它不含任务清单。
    """
    granularity: str
    spec: str
    design: str
    tasks: str
    plan_md: str
    full_plan: str
    conversation_plan: str


# 轻量轨 plan.md 里任务清单小节的标题。宽松匹配：`##任务清单`（无空格）、
# `###  任务清单`、标题行尾随空白与附加说明都认——`## 任务清单（checkbox：…）`
# 正是 _enter_plan_mode / build_plan_mode_prompt 给模型的写法。Markdown 允许标题
# 前最多 3 个空格缩进，一并容忍。
_TASK_LIST_HEADING_RE = re.compile(r"^\s{0,3}#{2,}[ \t]*任务清单")
# 下一个标题（任意级别）= 小节结束。`#{1,}` 而不是 `#{2,}`：后随的 H1（`# …`）
# 同样是边界，漏掉它会让 H1 连同它后面整段散文一起被切出对话版计划。
_NEXT_HEADING_RE = re.compile(r"^\s{0,3}#{1,}")

# conversation_plan 的兜底文案。计划**不是**空的——空的是它的散文正文：轻量轨的
# plan.md 可能只有 `## 任务清单` 一节，切掉清单后什么都不剩，而清单已经物化进
# task_list。刻意不回退到 full_plan：那正是在「清单就是整份计划」这种情况下把清单
# 重新嵌回对话，撤销 conversation_plan 来自的那个修复。
_CHECKLIST_ONLY_PLAN = "计划正文只有任务清单，已物化进 task_list。"

# integration 失败时的结果文案。此前这里是 "Proceed with implementation."——与成功
# 路径一字不差，读起来就是「一切正常」。而实际发生的是：plan 系统里没有条目、
# session.plan_slug 没设、session/plan_linked 没发、**一条任务都没物化进 task_list**，
# 于是清单摘要不会常驻上下文、焦点任务的详细方案也不会自动注入——整个特性的 payoff
# 整份消失，唯一痕迹是 stderr 一行。
#
# 此时回滚不了：用户已经批准、permission_mode 已经切换、mode-changed 事件已经发出，
# 这些都跑在这段文案之前。能做的只有不把「正常」说给模型和用户听，并告诉模型它得
# 自己推进、自己跟踪步骤。
#
# 只用在「integration 试过但失败」的路径上。approval_fn 分支的 manual-execute 是
# **刻意**不做 integration（见模块 docstring 的行为保留项），那不是失败，用同一句话
# 会把这条信号淹进噪声里。
_PLAN_NOT_RECORDED = (
    "警告：计划未能记录进 plan 系统，任务也未能物化进 task_list——"
    "清单摘要不会常驻你的上下文，焦点任务的详细方案也不会自动注入。\n"
    "请手动推进实现，并自己用 task_list add/update 跟踪每一步。\n"
)


def _strip_task_checklist_section(plan_md: str) -> str:
    """删掉轻量轨 plan.md 里的 `## 任务清单` 小节：标题行 → 下一个任意级标题或文末。

    找不到该标题时**逐字原样返回**——不动重量轨（它没有这个标题，任务清单在独立的
    tasks.md 里，由 conversation_plan 的组装直接略过 `## Tasks` 段），也不动没写
    清单小节的 plan。

    按行扫描而不是一个大 re.S 正则：小节边界是「下一个标题」，行扫描能把「标题行
    本身的宽松匹配」与「边界判定」分开写，两者各自可读。
    """
    lines = plan_md.split("\n")
    start: int | None = None
    end: int | None = None
    for index, line in enumerate(lines):
        if start is None:
            if _TASK_LIST_HEADING_RE.match(line):
                start = index
        elif _NEXT_HEADING_RE.match(line):
            end = index
            break
    if start is None:
        return plan_md
    return "\n".join(lines[:start] + (lines[end:] if end is not None else []))


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

    # 轻量轨的任务清单是 plan.md 里的 `## 任务清单` 一段，只能按标题切出来。切不动时
    # （模型把标题写成 `## Tasks`、`## 任务`，或者压根不写标题）_strip_task_checklist_
    # section 逐字原样返回，于是 conversation_plan == full_plan：整份 checkbox 清单重新
    # 进对话，正是 conversation_plan 这个设计要消掉的那笔重复账。
    # 刻意**不**加标题名启发式——猜名字猜错就会切掉一段合法散文，代价比重复一遍清单更
    # 高。这里只把缺口变成一条可观测的诊断：无声的重复没人会去修，可测量的重复才有
    # 「到底需不需要名字变体规则」的真数据。
    stripped_plan_md = _strip_task_checklist_section(plan_md)
    if granularity == "minimal" and tasks_content.strip() and stripped_plan_md == plan_md:
        print_error(
            "[plan] plan.md has a checkbox checklist but no recognized `## 任务清单` "
            "heading, so the conversation copy of the plan still contains the full list"
        )
        trace_event(
            "plan_mode.checklist_section_unrecognized",
            metadata={
                "granularity": granularity,
                "plan_md_chars": len(plan_md),
                "task_blocks": tasks_content.count("### Task"),
            },
        )

    full_plan = "\n\n".join(
        part for part in (
            plan_md.strip() if granularity == "minimal" and plan_md.strip() else "",
            f"## Spec\n{spec_content}" if spec_content.strip() else "",
            f"## Design\n{plan_content}" if plan_content.strip() else "",
            f"## Tasks\n{tasks_content}" if granularity != "minimal" and tasks_content.strip() else "",
        ) if part
    ) or "(empty plan)"
    # 对话版：与 full_plan 同源的零件，只是**不含任务清单**。
    # 重量轨：省掉 `## Tasks\n{tasks_content}` 那一段即可。
    # 轻量轨（默认且占多数）：full_plan 就是 plan_md.strip()，任务清单是 plan.md
    #   里的 `## 任务清单` 一段，**没有 `## Tasks` 标题可省**——只删 `## Tasks`
    #   的「修复」在这条轨上静默无效，必须按标题把那段切出来。
    conversation_plan = "\n\n".join(
        part for part in (
            stripped_plan_md.strip()
            if granularity == "minimal" and plan_md.strip() else "",
            f"## Spec\n{spec_content}" if spec_content.strip() else "",
            f"## Design\n{plan_content}" if plan_content.strip() else "",
        ) if part
    ) or _CHECKLIST_ONLY_PLAN
    return _PlanDraft(
        granularity, spec_content, plan_content, tasks_content, plan_md,
        full_plan, conversation_plan,
    )


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


def _clear_draft_dir(plan_dir: Any, slug: str) -> None:
    """批准落地后清掉草稿目录 `<workspace>/.mycode/plans/plan-<session_id>`。

    它此前**从不被清**——_finalize_plan_exit 只把 `mgr.plan_dir` 置 None，那条路径上
    没有任何 rmtree/unlink。而这个路径由 session_id 决定，每轮进 plan 模式都回到同一
    个目录；提示词又明教「追加模式」就地追加（plan_mode.py:127-128），轻量轨的转换器
    会把幸存 plan.md 里的**每个** checkbox 重新转一遍——于是第二轮 draft.tasks 几乎
    必然是「旧 + 新」，上一轮已物化过的任务被再物化一遍（新 id + pending）。

    只在 integration 成功后调用：失败时（slug 碰撞被 plan_mode.py:359-361 吞掉返回
    None）草稿是计划的唯一副本，删了就没了。

    清理失败不能让一次已经生效的批准返回错误文本，所以**整个函数体**都在 try 里、
    一律吞掉异常只留痕。调用点跑在 `agent.permission_mode = target_mode`、
    `mgr.plan_dir = None` 与 `_emit_permission_mode_event()` 之前，本函数里任何一行
    抛出去都会把批准拦腰打断（模式没切、事件没发、结果消息没回）。
    """
    if not plan_dir:
        return
    draft = Path(plan_dir)
    try:
        # 防御：草稿目录与 plan 系统目录同在 `.mycode/plans/` 下。兜底 slug 曾是
        # `plan-{session_id}`——与草稿目录逐字节同名，于是 create_plan 必抛、integration
        # 必返回 None，批准在默认轨道上静默什么都不做（现已改成互斥的
        # `approved-{session_id}`，见 plan_mode.py 兜底处的注释）。同名不再可能，但守卫
        # 留着：要删的是用户那份计划的唯一副本，把不变量写实比靠推理省事更安全。
        # 这行守卫**也**在 try 里：resolve()/get_plans_dir() 抛的话自撞与否就是未知的，
        # 未知时保留草稿是唯一安全的那一边，所以直接落到 except 留痕返回、不 rmtree。
        if slug and draft.resolve() == (get_plans_dir() / slug).resolve():
            print_error(f"[plan] draft dir IS the plan entry, refusing to clear: {draft}")
            return
        shutil.rmtree(draft, ignore_errors=True)
        if draft.exists():
            # ignore_errors=True 不抛，所以只能事后核对：残留的草稿下一轮照样会被复读。
            print_error(f"[plan] draft dir survived cleanup: {draft}")
    except Exception as e:
        print_error(f"[plan] failed to clear draft dir '{draft}': {e!r}")


def _finalize_plan_exit(agent: "Agent", mgr: Any, draft: _PlanDraft, choice: str) -> str:
    """批准路径：integration 落地 + 模式切换 + trace 上报 + 结果消息组装。"""

    execute = choice in ("clear-and-execute", "execute")
    # 行为保留：approval_fn 分支仅 execute/clear-and-execute 做 integration；
    # confirm 分支（含 manual-execute）始终做 integration。
    # 这个布尔值在尾部还要用一次：plan_result 为假有两种完全不同的成因——「试过但
    # 失败」必须在对话里响，「按设计跳过」不该响。
    integration_attempted = execute or mgr.plan_approval_fn is None
    if integration_attempted:
        plan_result: dict[str, Any] | None = mgr.handle_plan_system_integration(
            draft.spec, draft.design, draft.tasks, agent.session,
            granularity=draft.granularity, plan_md=draft.plan_md,
        )
    else:
        plan_result = None

    saved_plan_dir = mgr.plan_dir
    target_mode = "acceptEdits" if execute else (mgr.pre_plan_mode or "default")
    # 「已批准的计划文档在哪」：integration 成功时是 plan 系统目录（add_artifact 真正
    # 落盘的地方）。草稿目录随后就被清掉，消息里再指它就是指一个不存在的路径。
    if plan_result and plan_result.get("slug"):
        plan_home = get_plans_dir() / plan_result["slug"]
        _clear_draft_dir(saved_plan_dir, plan_result["slug"])
    else:
        plan_home = saved_plan_dir

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
        result_msg += f"Plan directory: {plan_home}\n\n"
    else:
        print_info(f"Plan approved. Executing in {target_mode} mode.")
        result_msg = f"User approved the plan. Permission mode: {target_mode}\n\n"
        if plan_result:
            result_msg += f"Plan system entry created: {plan_result.get('slug', '')}\n\n"
    result_msg += f"## Approved Plan:\n{draft.conversation_plan}\n\n"

    if plan_result and plan_result.get("slug"):
        plan_slug = plan_result["slug"]
        # 追加路径只物化本次新增的那一块。链条是确定性的、不是奇异情况：
        # session.plan_slug 只设不清（跨 context clear、跨进程重启存活），而 Task 3
        # 之后**再没有任何东西推进 plan 状态**（start_plan_execution 不再被调用，
        # create_plan 写的是 PROPOSED），PROPOSED/IN_PROGRESS/COMPLETED 全都过
        # handle_plan_system_integration 里 `not in ("archived", "abandoned")` 那道闸
        # → 必走 append_tasks_to_plan，把新旧任务拼成一份 tasks.md。此时按 slug 读
        # 会读到合并体，上一轮已物化过的任务被再物化一遍（新 id + pending，它的
        # tasks.md 标记按设计从批准那刻起就是冻结的）——task_list 是执行状态的唯一
        # 载体，翻倍就意味着常驻摘要 S 每个请求都背着 N 条陈旧重复（正是本设计要
        # 消灭的那笔 token 账），而 find_focus 还可能落到已完成工作的 pending 克隆
        # 上、把它的 detail 重新注入、邀模型把活重做一遍。
        # draft.tasks 正是 integration 刚追加进文件的那一段文本（轻量轨是
        # checkbox → 结构化的转换结果，_validate_and_load_draft 里已经转好）。
        tasks_md = draft.tasks if plan_result.get("action") == "appended" else None
        materialized, complete = _materialize_plan_into_task_list(
            agent.session.id, plan_slug, tasks_md=tasks_md)
        print_info(
            f"Plan approved: materialized {materialized} tasks into task_list "
            f"({plan_slug}, action={plan_result.get('action', 'created')}, complete={complete})"
        )
        if materialized:
            result_msg += (
                f"{materialized} 条任务已物化进 task_list。\n"
                "清单摘要常驻你的上下文尾部，无需调 list 查询进度。\n"
                f"{_injection_line(agent.session.id)}"
                f"已批准的计划文档冻结在 {plan_home}，此后活状态只在 task_list。"
            )
        else:
            result_msg += "Proceed with implementation."
        if not complete:
            # 截断只对 stderr 说是不够的：模型读到「N 条任务已物化」会当成最终结果，
            # 而 store 里可能只有前缀。刻意放在 `if materialized:` **外面**——读取/
            # 解析就抛的那种截断是 complete=False 且 count=0，嵌在里面会被整个吞掉，
            # 模型只剩 "Proceed with implementation."，于是当没有计划、直接开工。
            result_msg += (
                "\n注意：物化中途中断，task_list 可能不完整，"
                "开工前先用 task_list list 核对。"
            )
    elif integration_attempted:
        # plan_result 为假 = integration 试过但什么都没落地（handle_plan_system_
        # integration 返回 None）。此前这里发的也是 "Proceed with implementation."，
        # 与成功路径一字不差：整个特性的 payoff 静默消失，模型和用户都被告知一切正常。
        # 缘由与不可回滚的理由见 _PLAN_NOT_RECORDED。
        #
        # 这里再留一行痕是必要的，不是重复：integration 有好几条 return None 的路径
        # （plan_md 为空、转换不出 `### Task`、重量轨没有 tasks_content）**不打任何
        # 日志**，只有这一行能让它们同样可观测。
        print_error(
            f"[plan] integration recorded nothing for session '{agent.session.id}' "
            f"(choice={choice}); no task was materialized into task_list"
        )
        result_msg += _PLAN_NOT_RECORDED
    else:
        result_msg += "Proceed with implementation."

    return result_msg


def _injection_line(session_id: str) -> str:
    """结果消息里「详细方案会自动注入」那一句——只在它真会成真时才说。

    轻量轨的 checkbox 不带缩进子项时也能通过校验（validate_plan_artifacts 只要求
    task_count > 0），转换器于是只产出 `### Task N: 描述` + `- **状态**: …`，两者
    都被 body 收集排除 → _compose_detail 返回 "" → needs_disclosure 对空 detail 恒为
    False → **什么都不会注入**。承诺一次不会发生的注入，模型就会干等。

    判据取 find_focus 而不是「第 1 条」：ensure_focus_detail_visible 正是用
    find_focus 挑注入对象（in_progress > failed > pending），焦点条未必是物化出来的
    第一条——清单里可能已有别的任务，或首条在 tasks.md 里本就是 done。

    探测失败时返回空串（什么也不承诺）：这一段在 permission_mode 已切换、
    plan_dir 已清空、mode-changed 事件已发出**之后**，抛出去会让一次已经生效的
    批准返回错误文本。
    """
    try:
        focus = find_focus(list_tasks(session_id))
    except Exception as e:
        print_error(f"[plan] focus probe failed for '{session_id}': {e!r}")
        return ""
    if focus is not None and focus.detail.strip():
        return "焦点任务的详细执行方案会在下一次模型调用时自动注入。\n"
    return "焦点任务没有记录详细执行方案——开工前先自行确定做法。\n"


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


def _materialize_plan_into_task_list(
    session_id: str, slug: str, tasks_md: str | None = None
) -> tuple[int, bool]:
    """把已批准 plan 的任务物化进 task_list。返回 (物化条数, 是否完整)。

    `tasks_md` 非 None 时解析**这段文本**而不是读盘上的 tasks.md。二次批准走追加
    路径时盘上那份已经是「旧 + 新」的合并体，只有本次新增的一块该被物化（缘由见
    _finalize_plan_exit 里那段注释）。空串是「本次没有新增」而不是「没给」，所以
    判据是 `is None`，不是真假——否则会静默退回读合并体，把旧任务再物化一遍。

    `complete=False` 表示中途出过异常、store 里可能只有前缀。返回裸 int 时调用方
    无从区分「完整」与「截断」，模型于是会把「N 条任务已物化」当成最终结果，而
    只有 stderr 说了实话——所以截断必须是返回值的一部分。

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
    complete = True
    previous_id: int | None = None
    try:
        tasks = parse_tasks_content(tasks_md) if tasks_md is not None else get_tasks(slug)
        for task in tasks:
            status = _PLAN_TO_TASK_STATUS.get(getattr(task, "status", "pending"), "pending")
            item = add_task(
                session_id,
                content=getattr(task, "description", "") or f"Task {getattr(task, 'id', count + 1)}",
                detail=_compose_detail(task),
                acceptance=getattr(task, "acceptance", "") or "",
                after_id=previous_id,
            )
            previous_id = item.id
            # 计数跟着「store 里真有什么」走：add_task 返回即已入库。此前 count += 1
            # 在 update_task **之后**，那里一失败就会漏计一条已在库的任务（差一条）。
            count += 1
            if status != "pending":
                update_task(session_id, item.id, status=status,
                            error=getattr(task, "error", "") or "")
    except Exception as e:
        complete = False
        print_error(f"[plan] materialize failed for '{slug}' after {count} task(s): {e!r}")
    return count, complete
