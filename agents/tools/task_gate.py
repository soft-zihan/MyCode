"""验收证据观察 —— 从事件日志读事实，不接受 agent 自报。

旧的 plan_task_done 要求 agent 自己传 {"command","exit_code","output_snippet"}，
而没有任何东西校验它：agent 可以直接编一个 exit_code: 0。那是纯荣誉制，还带反向
激励（硬闸门会诱导模型编一条 acceptance 来通过）。

这里反过来：agent 不传任何数据，闸门自己去事件日志里找证据。没有编造面，也没有
摩擦。代价是 tool_result_msg 必须落盘 outcome 与 tool_name（见 agents/core/context.py
的 append_tool_message；三段穿线的回归网在 tests/unit/test_outcome_threading.py）。

四个刻意的设计选择：
1. 警告而非拒绝 —— 硬闸门会诱导编造，且很多任务没有可跑的验收命令。
2. 判据是「有没有成功的 run_shell」，不是「命令文本是否匹配 acceptance」。模糊
   匹配会产生假指控（pytest tests/x.py 与 python -m pytest tests/x.py -q 语义
   相同、字面不同）。宁可漏报，不可误报——误报会训练模型忽略警告。
3. 扫 session.events 而非可见集：折叠隐藏了证据事件，但证据确实发生过，折叠不该
   让一条已完成的任务突然「变得没验证过」。
4. 只看 run_shell。项目里没有专用测试工具；将来有了就扩 _EVIDENCE_TOOLS。
"""

from __future__ import annotations

from typing import Any, Iterable

from agents.tools.task_store import TaskItem

_EVIDENCE_TOOLS = frozenset({"run_shell"})
_SUCCESS_OUTCOME = "success"


def can_produce_evidence(tool_names: Iterable[str]) -> bool:
    """这个 agent 有没有可能产出闸门认的证据 —— dispatcher 上膛前的判据。

    刻意按**工具集**判，而不是按「是不是子 agent」判：
    - 有 run_shell 的子 agent，闭包绑的是它自己的 session、list_tasks 按
      session.id 取键、子 agent 事件也照样落盘，闸门语义完全成立。按 subagent
      身份一刀切会白白关掉一个正确的检查。
    - 反过来，一个被授予 task_list 却没有证据工具的 agent **永远**满足不了判据，
      于是每次标 completed 都会被警告 —— 一台保证假阳性的机器，而误报正是训练
      模型忽略警告的那种失败。这条路可达：Plan 1 把 task_list 加进了
      subagent._sub_agent_excluded，但显式声明 `allowed-tools: task_list` 的
      自定义 agent 走白名单分支，而那条分支不与 _sub_agent_excluded 求交。

    与 _EVIDENCE_TOOLS 同源，不复制字面量 "run_shell"：将来扩白名单（设计选择 4）
    时这里自动跟着放宽，不会出现「闸门认新工具、上膛条件还只认 run_shell」的静默
    偏差 —— 那种偏差的表现形式正是误报。
    """
    return bool(_EVIDENCE_TOOLS.intersection(tool_names))


def has_successful_shell_since(session: Any, since_seq: int | None) -> bool:
    """since_seq 之后（严格大于）有没有成功的验证命令。

    since_seq 为 None 时扫全部事件——对应模型跳过 in_progress 直接标 completed
    的情况：没有可靠的区间起点，退化为全量扫描（宽松方向，宁漏勿误）。

    两个键都用 `.get(k, "")` 读：commit 4f0b5ac 之前落盘的事件**根本没有这两个
    键**（不是空串，是键缺席），磁盘上的旧会话必须照样能过闸门而不抛。

    空串是「没有证据」，不是「被拒绝」——它同时覆盖取消、中止与权限拒绝三种
    情况（权限拒绝路径刻意不存 outcome，因为那里没有 ToolExecutionResult，就地
    编一个词表值等于断言兄弟事件没有断言的事）。闸门只需要 `"" != "success"`。
    """
    for event in session.events:
        if event.get("type") != "tool_result_msg":
            continue
        if event.get("tool_name", "") not in _EVIDENCE_TOOLS:
            continue
        if event.get("outcome", "") != _SUCCESS_OUTCOME:
            continue
        seq = event.get("seq")
        if since_seq is not None and (not isinstance(seq, int) or seq <= since_seq):
            continue
        return True
    return False


def build_acceptance_warning(task: TaskItem) -> str:
    """标 completed 但查不到验证证据时的警告文案。

    刻意不是指控：留出「已经验证过」的余地。语气太硬模型会学会忽略它，
    那比没有闸门更糟。

    acceptance 读的是 store 里的全量条目，所以这里永远是完整命令、不带 `…`——
    这正是模型在常驻摘要 S 里看到截断串时的兜底。
    """
    return (
        f"#{task.id}「{task.content}」已标 completed，但从它开始到现在，事件日志里"
        f"没有成功的 run_shell 记录。它声明的验收命令是：{task.acceptance.strip()}\n"
        f"如果已经用别的方式验证过，忽略这条。否则先跑上面那条命令再继续。"
    )
