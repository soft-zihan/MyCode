"""plan 审批的解决路径（非阻塞审批的后端一半，spec §D3/§D4/§D9）。

审批不再阻塞在工具内部：turn 收尾落一条 permission/request{tool_name:"plan_approval"}
之后回合就正常结束了，**没有任何协程在等**。所以「用户点了批准」这件事必须由 HTTP
处理器把后续动作全部做完：落 permission/resolved、finalize（退出 plan 模式 / 落库 /
物化进 task_list / 清草稿目录）、再起一个新回合，其起始消息携带 spec §D8 的当前状态
声明与那段物化说明。

单一入口 `resolve_plan_approval`：/api/events/respond 的批准与拒绝分支、以及
PUT /auto-approve 的「审批已挂着才翻开开关」分支共用它。**不写两份**——两份必然漂移，
而漂移的后果是同一种用户操作在两条路上落地不一样（例如一条起新回合、另一条不起）。

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
from agents.plan.plan_tool_executor import finalize_approved_plan, record_plan_rejection

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
