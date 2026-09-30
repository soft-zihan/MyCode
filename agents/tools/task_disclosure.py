"""焦点任务 detail 的条件披露注入 —— 全系统唯一注入路径。

工具层（task_tools）刻意不注入：它拿不到自己那条 tool_result_msg 的 seq，无法
记账 detail_origin_seq，会导致下一轮重复注入。见 plan-1 Task 8 的说明。

为什么注入点只有一个、且必须在模型调用前而不是工具调用后：dispatcher 里注入会
让 memory_injection 落在 assistant(tool_calls) 与其 tool_result 之间，破坏工具
调用配对。放在 check_and_compact 之后，上一步的 tool_result 已落盘，顺序正确。
"""

from __future__ import annotations

from typing import Any

from agents.logging import print_error
from agents.tools.task_store import (
    find_focus,
    format_disclosure_block,
    list_tasks,
    mark_detail_disclosed,
    needs_disclosure,
)


def ensure_focus_detail_visible(session: Any) -> bool:
    """焦点条的 detail 若已不在可见上下文，注入一次 memory_injection 事件。

    幂等：注入后立刻把 detail_origin_seq 记为新事件 seq，而 `Session.append` 让新
    事件默认可见（events_hidden 类型除外），所以同一上下文状态下重复调用不会重复
    注入。被折叠隐藏后 needs_disclosure 再次为真，于是自愈式重注入。

    Returns:
        bool: 是否真的注入了（注入后被回滚也算没注入，返回 False）。
    """
    focus = find_focus(list_tasks(session.id))
    if not needs_disclosure(focus, session.visible_seqs):
        return False

    block = format_disclosure_block(focus)
    if not block:
        return False

    event = session.append("memory_injection", {"content": block})
    seq = event.get("seq")
    if not isinstance(seq, int):
        # 非 int seq 不能记账（写进 store 也会被 from_dict 的 isinstance 收窄回
        # None）。仍算注入成功：事件已落盘且默认可见，本轮模型看得到 detail。
        return True
    try:
        mark_detail_disclosed(session.id, focus.id, seq)
    except Exception as e:
        # 记账失败必须回滚注入：否则事件留在可见集里而 detail_origin_seq 未记录，
        # 此后每次模型调用都会重注入最多 6000 字符——本特性唯一一处无上界的浪费。
        # hide_events 会 bump surface generation（触发一次全量重派生），但这是
        # 失败路径，可接受。
        session.hide_events([seq])
        print_error(f"[task_disclosure] bookkeeping failed, injection rolled back: {e!r}")
        return False
    return True
