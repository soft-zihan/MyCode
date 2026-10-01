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
import traceback
from typing import TYPE_CHECKING

from agents.core.prompt import build_system_prompt_with_breakdown
from agents.core.workspace import set_workspace, reset_workspace
from agents.logging import print_error
from agents.tools.task_store import (
    TaskItem,
    find_focus,
    format_task_list_block,
    list_tasks,
)

if TYPE_CHECKING:
    from agents.agent import Agent


def build_tail_system_messages(
    agent: "Agent", tasks: list[TaskItem] | None = None
) -> list[str]:
    """请求尾部 ephemeral system messages（每请求重建，不进主 prompt）。

    顺序：plan 模式提示词在前（模式级约束），清单摘要 S 居中（任务变更时才变），
    fold guidance 在后（最易变——利用率百分比每请求都变）。更易变的放最后，
    前面的部分才能在多请求间尽量稳定。

    Args:
        tasks: **每请求快照**（Plan 3b Task B1）。生产路径由 ModelCaller._attempt
            读一次并显式传下来，与折叠门探针、推式披露共用同一份清单。None = 没有
            快照（直接调用方、既有测试，或快照加载失败）→ 自己读一次。
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
    # 这次读**默认不发生**：清单由 ModelCaller._attempt 每请求读一次、作为快照传进来
    # （Plan 3b Task B1）。此前这里是每请求的第二次 store 读（另一次是
    # ensure_focus_detail_visible → list_tasks），当时的裁定写的是「已评估并接受，
    # 别再重新论证」，理由是共享一次加载要让 task_disclosure 与 prompt_runtime 跨
    # 模块耦合，而两次读只花 ~0.22 ms。那条裁定成立的前提是**只有一个写入方**。
    # UI 写端点落地后，一次写入可以落在折叠门探针、推式披露与 S 的三次独立读之间，
    # 于是「折叠门认为没有任务在跑、模型眼前的 S 认为有」成了可读出来的状态——这正是
    # Plan 2 同时写下的另一半裁定所要防的：「Plan 3 引入第二个写入方时，每请求只读
    # 一次、把快照传给三个消费者」。这里就是那半个裁定的落点。
    # 跨请求缓存仍然禁止（Plan 2 反对的是那件事：拿不可达的竞态换可达的陈旧 bug）；
    # 请求作用域的快照没有失效逻辑，不在那条禁令里。
    #
    # tasks is None 的回退分支保留原来那次读，连带它的两个副作用：get_tasks_dir()
    # 每次调用都 mkdir（幂等），load_tasks() 遇到坏文件会把它改名成
    # {stem}.corrupt-{ts}.json 隔离掉再返回空清单。两处行为都刻意保留（隔离是防
    # 「读失败退化成空清单 → 下次 save 覆盖全文件」的丢数据保护，mkdir 幂等且极便宜），
    # 此处只是不把副作用藏进一句轻描淡写的注释里。走快照的生产路径上这两个副作用发生在
    # try_list_tasks 那一次读里，每请求一次而不是三次。
    #
    # try/except：尾部组装绝不能抛——它每次模型调用都跑，store 读失败只应退化成
    # 「这次没有任务块」，不该阻断模型调用（与 model_caller 的披露调用点同一策略）。
    try:
        items = tasks if tasks is not None else list_tasks(agent.session.id)
        if items:
            task_block = format_task_list_block(items, find_focus(items))
            if task_block:
                tails.append(task_block)
    except Exception as e:
        # 带 traceback：S 组装路径里的编程错误不该只以一个裸 repr 现身——这条
        # 路径每次模型请求都跑，静默退化会让整个常驻层消失，而裸 repr 连是哪一
        # 行都看不出来（与 model_caller 的披露调用点同一措辞、同一理由）。
        # 仍然吞掉——尾部组装绝不能阻断模型调用。
        print_error(
            f"[task_list] tail block failed: {e!r}\n{traceback.format_exc()}"
        )

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
