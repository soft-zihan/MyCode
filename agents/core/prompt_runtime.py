"""运行时 prompt 组装（U0 从 Agent 拆出，U5a 真冻结落点）。

两个关注点：
- build_tail_system_messages：请求尾部 ephemeral system messages，每次调用重拼，
  不进主 system prompt（prefix cache 保护）。含 plan 模式提示词（U5a：从主
  prompt 迁出，对齐 v2 plugin/plan.ts 的"每请求对账重注入且贴近尾部"）、任务
  清单摘要 S（直接读 task_store 投影，不经 LLM 摘要）与运行时易变状态
  （上下文利用率/错误连击/fold 时间）。
- refresh_runtime_system_prompt：system[0] 会话内真冻结（所有模式），仅
  force（skill_create、/cd 等结构性变化）时重建。plan 进出不触发重建——
  模式切换不再破坏 prefix cache，也不再触发消息全量重派生
  （session.system_prompt setter 会 bump surface generation）。
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from agents.core.prompt import build_system_prompt_with_breakdown
from agents.core.workspace import set_workspace, reset_workspace
from agents.logging import print_error
from agents.tools.task_store import find_focus, format_task_list_block, list_tasks

if TYPE_CHECKING:
    from agents.agent import Agent


def build_tail_system_messages(agent: "Agent") -> list[str]:
    """请求尾部 ephemeral system messages（每请求重建，不进主 prompt）。

    顺序：plan 模式提示词在前（模式级约束），清单摘要 S 居中（任务变更时才变），
    fold guidance 在后（最易变——利用率百分比每请求都变）。更易变的放最后，
    前面的部分才能在多请求间尽量稳定。
    """
    tails: list[str] = []
    if agent._custom_system_prompt is not None:
        return tails

    if agent.permission_mode == "plan" and agent._plan_mode_manager is not None:
        plan_prompt = agent._plan_mode_manager.build_plan_mode_prompt()
        if plan_prompt:
            tails.append(plan_prompt.strip())

    # 任务清单摘要 S：常驻、每请求重拼、不持久化。直接读 task_store 投影，不经
    # LLM 摘要（spec §七 原则：有权威结构化存储的东西靠读存储进上下文）。已完成
    # 条目只报计数，所以 S 随计划推进而收缩。刻意不含 detail —— detail 走
    # task_disclosure 的条件注入，常驻块带上它就等于放弃渐进式披露。
    #
    # 这是每请求的第二次 store 读（另一次是 model_caller 里
    # ensure_focus_detail_visible → list_tasks）。**已评估并接受，别再重新论证**：
    # 两次读的是同一个小 JSON——实测 5 条清单 2 KB、11 条带长 detail 的清单
    # 11.5 KB，单次 list_tasks ≈ 0.11 ms，两次 ≈ 0.22 ms，相对一次模型调用
    # （秒级）是万分之一量级。共享一次加载要让 task_disclosure 与 prompt_runtime
    # 跨模块耦合，省下的是测不出来的时间；mtime 缓存则是在没有实测证据前先引入
    # 失效逻辑（推测性复杂度）。若将来真测出成本，落点应是一个带失效语义的
    # store 读取层，而不是把这两个调用点缝在一起。
    #
    # try/except：尾部组装绝不能抛——它每次模型调用都跑，store 读失败只应退化成
    # 「这次没有任务块」，不该阻断模型调用（与 model_caller 的披露调用点同一策略）。
    try:
        tasks = list_tasks(agent.session.id)
        if tasks:
            task_block = format_task_list_block(tasks, find_focus(tasks))
            if task_block:
                tails.append(task_block)
    except Exception as e:
        print_error(f"[task_list] tail block failed: {e!r}")

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
        agent._system_prompt_breakdown = {}
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
    _ws_token = set_workspace(agent.workspace)
    try:
        active_tools = {t.get("name") for t in agent.tools if t.get("name")}
        # BC-23：渲染时一次性产出分段字符数并缓存——观测方只读缓存，
        # 不再每次模型调用重建 claude_md/wiki/skills/workspace 重资产
        prompt, breakdown = build_system_prompt_with_breakdown(active_tools=active_tools)
        agent._base_system_prompt = prompt
        agent._system_prompt = prompt
        agent._system_prompt_breakdown = breakdown
        agent.session.system_prompt = agent._system_prompt
    finally:
        reset_workspace(_ws_token)
