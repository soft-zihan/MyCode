"""运行时 prompt 组装（U0 从 Agent 拆出，U5a 分层提示词重构的落点）。

两个关注点：
- build_runtime_guidance：请求尾部 ephemeral system message，每次调用重拼，
  不进主 system prompt（prefix cache 保护）。
- refresh_runtime_system_prompt：system[0] 重建——普通模式下会话内冻结；
  plan 模式（计划状态需实时）与 force（skill_create、/cd 等结构性变化）例外。
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from agents.core.prompt import build_system_prompt

if TYPE_CHECKING:
    from agents.agent import Agent


def build_runtime_guidance(agent: "Agent") -> str | None:
    """运行时易变状态（上下文利用率/错误连击/fold 时间）。

    作为请求尾部 ephemeral system message 注入，不写入主 system prompt——
    主 prompt 任何位置变化都会使其后全部历史的 prefix cache 失效。
    """
    if agent._custom_system_prompt is not None:
        return None
    utilization = agent.estimated_context_tokens / agent.effective_window if agent.effective_window else 0.0
    last_fold = "never" if not agent._fold_last_time else f"{int((time.time() - agent._fold_last_time) / 60)}m ago"
    return (
        "# Runtime Fold Guidance\n"
        f"- Current context utilization: {utilization:.0%}\n"
        f"- Recent tool error streak: {agent._tool_error_streak}\n"
        f"- Same tool repeat count: {agent._same_tool_repeat_count}\n"
        f"- Last fold: {last_fold}\n"
        "- If the context is getting long, the same tool is being retried without progress, or tool failures are accumulating, call `compact_context` before trying more tools.\n"
        "- If you folded very recently and the next step is clear, prefer continuing rather than folding again."
    )


def refresh_runtime_system_prompt(agent: "Agent", force: bool = False) -> None:
    if agent._custom_system_prompt is not None:
        agent.session.system_prompt = agent._custom_system_prompt
        return
    # prefix cache 保护：普通模式下 system prompt 会话内冻结，不随 step 重建
    # （build_system_prompt 重读 wiki index/workspace 结构，且旧实现把易变的
    # fold guidance 拼进 prompt 末尾，导致每次调用前缀都不同、cache 率 <15%）。
    # plan 模式例外：计划状态需实时反映。force 用于结构性变化（skill_create、/cd）。
    if not force and agent.permission_mode != "plan" and agent.session.system_prompt:
        return
    from agents.core.workspace import set_workspace, reset_workspace
    _ws_token = set_workspace(agent.workspace)
    try:
        agent._base_system_prompt = build_system_prompt()
        if agent.permission_mode == "plan":
            agent._system_prompt = agent._base_system_prompt + agent._plan_mode_manager.build_plan_mode_prompt()
        else:
            agent._system_prompt = agent._base_system_prompt
        agent.session.system_prompt = agent._system_prompt
    finally:
        reset_workspace(_ws_token)
