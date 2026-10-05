"""plan 审批的解决路径（非阻塞审批的后端一半，spec §D3/§D4/§D9）。

审批不再阻塞在工具内部：turn 收尾落一条 permission/request{tool_name:"plan_approval"}
之后回合就正常结束了，**没有任何协程在等**。所以「用户点了批准」这件事必须由 HTTP
处理器把后续动作全部做完：落 permission/resolved、finalize（退出 plan 模式 / 落库 /
物化进 task_list / 清草稿目录）、再起一个新回合，其起始消息携带 spec §D8 的当前状态
声明与那段物化说明。

单一入口 `resolve_plan_approval`：/api/events/respond 的批准与拒绝分支、以及
PUT /auto-approve 的「审批已挂着才翻开开关」分支共用它。**不写两份**——两份必然漂移，
而漂移的后果是同一种用户操作在两条路上落地不一样（例如一条起新回合、另一条不起）。
`rebuild_pending_plan_approval`（spec §十.6：后端重启后内存态丢失）同样**不落地任何
审批动作**，它只负责把 pending 从盘上重建出来，然后交回同一个 `resolve_plan_approval`。

⚠️ 整个函数体必须包在 `workspace_scope(resolve_session_workspace(...))` 里：
finalize 会落 plan 系统条目（get_plans_dir）并物化进 task_list（task_store 的目录
来自 get_workspace() 这个 ContextVar），而 HTTP 请求不在任何会话的 scope 里。不包
这一层就是 W1 那个缺陷的翻版：端点回 200，东西写进 project_root 下的幽灵文件，
Agent 永远读不到。
"""

from __future__ import annotations

import logging
from typing import Any

from agents.core.workspace import workspace_scope
from agents.plan.plan_tool_executor import (
    PLAN_APPROVAL_TOOL_NAME,
    _validate_and_load_draft,
    finalize_approved_plan,
    record_plan_rejection,
)

from .chat import _spawn_chat_task
from .sessions import resolve_session_workspace

logger = logging.getLogger(__name__)

# 前端没给 choice 时的默认批准路由（自动审批开关那条路也用这个：spec §D9 说的是
# 「按批准并自动执行结掉」，与权限门自动放行时 last_choice 为空 → "execute" 同口径）。
DEFAULT_APPROVAL_CHOICE = "execute"


def resolve_plan_approval(
    sm: Any,
    agent: Any,
    pending: dict,
    *,
    allowed: bool,
    choice: str = "",
    feedback: str = "",
    outcome: str | None = None,
) -> dict[str, Any]:
    """落地一次 plan 审批的应答。

    调用方必须已经成功 `agent.take_pending_plan_approval(...)`（取出即清空，所以
    双击/重试第二次拿不到 pending、走不到这里，同一批任务不会被物化两遍）。

    outcome 省略时按 allowed 取 "allowed"/"denied"；自动审批开关那条路显式传 "auto"
    （spec §D9：请求已落盘，决议就必须落盘，否则历史恢复会把它复活成幽灵对话框）。

    失败方向：resolved **先**落盘再 finalize——finalize 抛异常时也不能让历史里留下
    一条没有配对的 request。异常本身不静默：stderr 一条 + 事件流一条 error，并把
    success=False 回给前端（C1 缺陷类的教训：静默降级 + 上层成功信号最贵）。
    """
    session = agent.session
    rpc_id = pending.get("rpc_id", "")
    resolved_outcome = outcome or ("allowed" if allowed else "denied")

    with workspace_scope(resolve_session_workspace(session.id)):
        session.append("permission/resolved", {
            "rpc_id": rpc_id,
            "outcome": resolved_outcome,
        })

        try:
            if allowed:
                message = finalize_approved_plan(
                    agent, pending["draft"], choice or DEFAULT_APPROVAL_CHOICE)
            else:
                # 拒绝：留在 plan 模式（finalize 没被调用，模式一步都没动），
                # 意见原文作为新回合的起始消息喂回去——那是模型修改计划的唯一输入。
                message = record_plan_rejection(agent, feedback)
        except Exception as exc:
            detail = f"{type(exc).__name__}: {exc}"
            logger.error(f"[plan_approval] finalize failed for rpc_id={rpc_id}: {detail}")
            session.append("error", {
                "message": f"计划审批落地失败（未起执行回合）: {detail}",
            })
            return {"success": False, "message": detail}

        # 起新回合：复用 chat.py 的机器（_spawn_chat_task 建任务 + register_chat_task
        # 登记，于是 stop/cancel 仍然管得到它），不自己造一套。
        _spawn_chat_task(sm, agent, session, message)

    return {"success": True, "outcome": resolved_outcome}


def rebuild_pending_plan_approval(agent: Any, rpc_id: str) -> dict[str, Any] | str | None:
    """spec §十.6：后端重启后从盘上重建「仍然活着的计划审批」。

    `Agent._pending_plan_approval` 是内存态，重启就没了；而前端照旧能从历史恢复出
    审批对话框（restorePendingPermission.ts：有 permission/request 没有配对的
    permission/resolved）。此前用户点批准会掉进 /api/events/respond 末尾那条
    `set_permission_response`——那里没有任何等待者，于是**什么都不发生、也不报错**：
    UI 显示成功、实际无效、零可观测，本项目最恨的那个形状。

    修法是重建而不是加持久化：判据与前端那条恢复逻辑**同一条**（会话事件里这条
    rpc_id 的 plan_approval 请求没有配对的 resolved），计划正文本来就还在
    `{plan_dir}/plan.md` 上，重新装载 + 重新校验就行。校验和装载都复用
    `_validate_and_load_draft`（agent_loop 送审时用的同一个函数），落地复用
    `resolve_plan_approval`——一条路上不写第二份。

    Returns:
        dict: 重建出的 pending，直接喂给 resolve_plan_approval。只放它会读的键
              （rpc_id / draft）；`choice_options` 是内存态那份给 UI 用的，这里没有
              消费者，多存一份就是多一个漂移点。
        str:  明确错误，调用方要回给前端显示（例如用户在重启前后把 plan.md 手工改坏了，
              此时重新校验不合格）。**绝不用 None 表示失败**——None 有别的含义。
        None: 这个 rpc_id 与计划审批无关（危险命令确认，照旧走权限门那条路），
              或者它已经被决过了（批准/拒绝/superseded/自动放行）。
              「已决 → None」与 take_pending_plan_approval 的「取出即清空」是同一种
              幂等：双击、前端重试第二次走到这里时不能再 finalize 一遍，
              否则同一批任务会被物化进 task_list 两次。
    """
    events = agent.session.events
    if not any(
        e.get("type") == "permission/request"
        and e.get("rpc_id") == rpc_id
        and e.get("tool_name") == PLAN_APPROVAL_TOOL_NAME
        for e in events
    ):
        return None
    if any(
        e.get("type") == "permission/resolved" and e.get("rpc_id") == rpc_id
        for e in events
    ):
        return None

    with workspace_scope(resolve_session_workspace(agent.session.id)):
        # 重启后 hydrate 出来的 agent 是新构造的：permission_mode 回到构造默认值、
        # plan_dir 是 None。而「有一条挂着的计划审批」就意味着这个会话当时正在 plan
        # 模式，所以走真的内部入口把它恢复回去（幂等：已经是 plan 就直接返回），
        # 顺带拿回草稿目录（generate_plan_dir 由 session_id 决定，与重启前逐字节同路径）
        # 并发出 permission/mode_changed——UI 于是显示真实模式，而不是重启后那个假的
        # 默认值。⚠️ pre_plan_mode 会记成 hydrate 出来的默认模式（真值已随重启丢失），
        # 只影响 manual-execute 这一条路由批准后的目标模式；为它加持久化不划算。
        agent._enter_plan_mode_internal()
        draft = _validate_and_load_draft(agent._plan_mode_manager)

    if isinstance(draft, str):
        logger.error(f"[plan_approval] rebuild revalidation failed for rpc_id={rpc_id}: {draft}")
        return (
            "计划没有通过格式校验，审批未落地（plan.md 可能在重启前后被改动过）。"
            f"校验器给出的原因：{draft}"
        )
    return {"rpc_id": rpc_id, "draft": draft}
