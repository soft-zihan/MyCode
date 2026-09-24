"""运行时 prompt 组装（U0 从 Agent 拆出，U5a 真冻结落点）。

两个关注点：
- build_tail_system_messages：请求尾部 ephemeral system messages，每次调用重拼，
  不进主 system prompt（prefix cache 保护）。含 plan 模式提示词（U5a：从主
  prompt 迁出，对齐 v2 plugin/plan.ts 的"每请求对账重注入且贴近尾部"）与
  运行时易变状态（上下文利用率/错误连击/fold 时间）。
- refresh_runtime_system_prompt：system[0] 会话内真冻结（所有模式），仅
  force（skill_create、/cd 等结构性变化）时重建。plan 进出不触发重建——
  模式切换不再破坏 prefix cache，也不再触发消息全量重派生
  （session.system_prompt setter 会 bump surface generation）。
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from agents.core.prompt import build_system_prompt

if TYPE_CHECKING:
    from agents.agent import Agent


def build_tail_system_messages(agent: "Agent") -> list[str]:
    """请求尾部 ephemeral system messages（每请求重建，不进主 prompt）。

    顺序：plan 模式提示词在前（模式级约束），fold guidance 在后（最易变）。
    """
    tails: list[str] = []
    if agent._custom_system_prompt is not None:
        return tails

    if agent.permission_mode == "plan" and agent._plan_mode_manager is not None:
        plan_prompt = agent._plan_mode_manager.build_plan_mode_prompt()
        if plan_prompt:
            tails.append(plan_prompt.strip())

    utilization = agent.estimated_context_tokens / agent.effective_window if agent.effective_window else 0.0
    last_fold = "never" if not agent._fold_last_time else f"{int((time.time() - agent._fold_last_time) / 60)}m ago"
    tails.append(
        "# Runtime Fold Guidance\n"
        f"- Current context utilization: {utilization:.0%}\n"
        f"- Recent tool error streak: {agent._tool_error_streak}\n"
        f"- Same tool repeat count: {agent._same_tool_repeat_count}\n"
        f"- Last fold: {last_fold}\n"
        "- If the context is getting long, the same tool is being retried without progress, or tool failures are accumulating, call `compact_context` before trying more tools.\n"
        "- If you folded very recently and the next step is clear, prefer continuing rather than folding again."
    )
    return tails


def refresh_runtime_system_prompt(agent: "Agent", force: bool = False) -> None:
    if agent._custom_system_prompt is not None:
        agent._base_system_prompt = agent._custom_system_prompt
        agent._system_prompt = agent._custom_system_prompt
        agent.session.system_prompt = agent._custom_system_prompt
        return
    # U5a 真冻结：system prompt 会话内不变（所有模式，含 plan）。
    # build_system_prompt 重读 wiki index/workspace 结构，任何重建都使
    # prefix cache 全量失效；plan 状态已迁至尾部 ephemeral 通道，
    # 每请求对账重注入，无需重建主 prompt。force 仅用于结构性变化
    # （skill_create、/cd、MCP 工具接入）。工具指引按 agent.tools 快照
    # 条件生成（v2 system-prompt.ts 模式）；force 重建若字节相同，
    # session.system_prompt setter 不 bump generation，缓存无损。
    if not force and agent.session.system_prompt:
        return
    from agents.core.workspace import set_workspace, reset_workspace
    _ws_token = set_workspace(agent.workspace)
    try:
        active_tools = {t.get("name") for t in agent.tools if t.get("name")}
        agent._base_system_prompt = build_system_prompt(active_tools=active_tools)
        agent._system_prompt = agent._base_system_prompt
        agent.session.system_prompt = agent._system_prompt
    finally:
        reset_workspace(_ws_token)
