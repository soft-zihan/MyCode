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

from typing import Any

from agents.tools.task_store import TaskItem

_EVIDENCE_TOOLS = frozenset({"run_shell"})
_SUCCESS_OUTCOME = "success"


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
