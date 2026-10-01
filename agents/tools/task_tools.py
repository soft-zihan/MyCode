"""task_list 工具 — Agent 任务追踪与渐进式披露。

Plan 模式下禁用（撰写阶段的产物要进 PlanApprovalDialog 供审批，直接写 task_list
会绕过审批闸门）。plan 批准后任务由 _finalize_plan_exit 物化进来。

三层可见性（详见 deliverables/task-list-context-analysis.md §3）：
- 常驻：清单摘要走尾部 ephemeral 通道（prompt_runtime.build_tail_system_messages）
- 推：焦点条 detail，仅当它已不在可见上下文时注入（task_disclosure）
- 拉：list 只给摘要，get 给单条完整 detail
"""

from __future__ import annotations

import json
from typing import Callable

from agents.logging import print_error
from agents.tools.task_gate import build_acceptance_warning
from agents.tools.task_store import (
    TASK_STATUS_COMPLETED,
    TASK_STATUS_IN_PROGRESS,
    VALID_STATUSES,
    TaskItem,
    add_task,
    find_focus,
    list_tasks,
    remove_task,
    update_task,
)

TASK_LIST_TOOL = {
    "name": "task_list",
    "description": (
        "管理任务清单，是多步任务的唯一进度载体。清单摘要常驻你的上下文尾部，"
        "无需反复调 list 查询。\n\n"
        "何时用：任务需要 3 步以上、或用户给了多个待办时，立即用 add 建清单。\n"
        "怎么写：content 保持一句话；把怎么做/为什么/注意事项写进 detail；"
        "把可验证的完成判据（通常一条命令）写进 acceptance。\n"
        "detail 只在该条成为焦点且已不在你上下文里时才自动注入，所以不必担心"
        "写长——但也不要为了写而写。\n"
        "声明了 acceptance 的任务，标 completed 时需要事件日志里有通过的验证命令。\n\n"
        "状态流转：pending → in_progress → completed；跳过用 skipped；失败用 failed "
        "并填 error。同一时间只应有一条 in_progress。\n"
        "绝不要在实现不完整、有未解决报错、找不到必要文件时标 completed。\n\n"
        "Plan 模式下禁用。"
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "operation": {
                "type": "string",
                "enum": ["add", "update", "remove", "list", "get"],
                "description": "操作类型",
            },
            "id": {
                "type": "integer",
                "description": "任务 ID（update/remove/get 时必需）",
            },
            "content": {
                "type": "string",
                "description": "一句话任务摘要（add 时必需，update 时可选）",
            },
            "detail": {
                "type": "string",
                "description": (
                    "详细执行方案：怎么做、为什么这样做、注意事项。"
                    "只在该条成为焦点且已不在上下文里时自动披露，"
                    "也可用 get 主动取。list 不返回此字段。"
                ),
            },
            "acceptance": {
                "type": "string",
                "description": (
                    "可验证的完成判据，通常是一条命令（如 pytest tests/unit/test_x.py）。"
                    "常驻在清单摘要里。声明了它的任务，标 completed 时需要事件日志里"
                    "有通过的验证命令。"
                ),
            },
            "status": {
                "type": "string",
                "enum": ["pending", "in_progress", "completed", "skipped", "failed"],
                "description": "任务状态（update 时可选）",
            },
            "error": {
                "type": "string",
                "description": "失败原因（status=failed 时填）",
            },
            "after_id": {
                "type": "integer",
                "description": (
                    "插入/移动到该 ID 之后。0 = 放到最前。不传 = 追加末尾（add）"
                    "或不动位置（update）。用于中途加一步与重排。"
                ),
            },
        },
        "required": ["operation"],
    },
}


# 每个操作真正消费的入参（设计 §六 的操作表）。schema 是一个扁平属性袋，
# 跨操作参数（add 带 status）与**已从 schema 删掉的旧字段**（priority，Plan 3a
# Task 1 删除）会被静默丢弃却仍回 ok:true。add/update 不再回显 detail 之后，
# 模型连「从回显里发现字段没生效」这条线索都没有了，所以把被忽略的键显式报回去。
# 把 "priority" 从 add 的集合里去掉正是这个机制的用途：模型按旧 schema 发过来的
# priority 会拿到一条 "ignored": ["priority"] 回显，而不是无声消失。
_KEYS_BY_OPERATION = {
    "add": {"operation", "content", "detail", "acceptance", "after_id"},
    "update": {"operation", "id", "content", "detail", "acceptance", "status",
               "error", "after_id"},
    "remove": {"operation", "id"},
    "list": {"operation"},
    "get": {"operation", "id"},
}


def summary_dict(item: TaskItem) -> dict:
    """摘要视图 —— 刻意不含 detail。`list` 的每条、`add`/`update` 的回显都用它。

    detail 只有一条工具出口：`get`（设计 §六）。若 add/update 也回显 detail，
    每次状态流转（pending → in_progress → completed，10 条计划约 20 次）都会把
    模型自己刚写的方案再拉回上下文一遍 —— 正是设计为 list 点名要防的「调一次
    list 就把所有方案拉进上下文」，只是换成逐条到达。而且它换不来记账正确性：
    回显落在 tool_result_msg 里，needs_disclosure 看的是 detail_origin_seq，
    那条旧事件被折叠后披露路径照样重新注入。

    error 不在设计 §六 的字段清单里，但设计另要求常驻摘要块带 failed 条的
    error 首行，且 error 只在 failed 时非空 —— 刻意保留（控制器已裁定）。

    priority 已删除（Plan 3a Task 1）：它没有任何读者，而摘要视图是每请求付费的
    常驻成本的近亲，不该为一个模型无法据此行动的字段留位置。
    """
    return {
        "id": item.id,
        "content": item.content,
        "status": item.status,
        "acceptance": item.acceptance,
        "error": item.error,
    }


def full_dict(item: TaskItem) -> dict:
    """get 用的完整视图 —— detail 进入上下文的唯一工具出口。"""
    return item.to_dict()


def _ok(payload: dict, ignored: list[str] | None = None,
        hint: str | None = None) -> str:
    body = {"ok": True, **payload}
    if ignored:
        body["ignored"] = ignored
    if hint:
        body["hint"] = hint
    return json.dumps(body, ensure_ascii=False, indent=2)


def _ignored_keys(operation: str, inp: dict) -> list[str]:
    """本次调用里对该操作无效的键（排序返回，输出确定）。"""
    valid = _KEYS_BY_OPERATION.get(operation)
    if not valid:
        return []
    return sorted(key for key in inp if key not in valid)


def _coerce_id(value) -> int | None:
    """id 类入参统一转 int；转不动返回 None，落到既有的 clean error 分支。

    schema 声明 integer，但模型有时写字符串，所以必须转：不转的话「int
    task_id + str after_id」会绕过 store 的自锚守卫 `after_id != task_id`，
    重现「静默甩到列表末尾」。这里也不直接 int(value)：非数字（"abc"）会抛
    ValueError，被 dispatcher 的宽 except 包成 "tool 'task_list' failed:
    ValueError: ..."，模型看到的是噪声；而且行为会随清单空/非空而不同（空清单
    在循环前就返回 not found）。返回 None 让两条路收敛到同一句 clean error。
    """
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _id_error(inp: dict, key: str) -> str | None:
    """key **提供了**却解析不成整数 → clean error；未提供或能解析 → None。

    _coerce_id 把 "abc" 静默收窄成 None，于是「提供了但坏掉」与「压根没提供」合流到
    同一个语义上，两处都因此说谎：
    - `after_id`：一次被请求的插入/移动悄悄不发生，回 ok:true，而且**不进 ignored**
      （after_id 对 add/update 都是合法键，_unknown_keys 认得它）。模型以为放对了
      位置，实际没有，且没有任何信号告诉它——静默失败比报错贵得多。
    - `id`：落到「id is required」，而模型确实发了 id；文案与事实矛盾，它于是会去补
      一个自己已经给了的东西。
    """
    raw = inp.get(key)
    if raw is None:
        return None
    try:
        int(raw)
    except (TypeError, ValueError):
        return f"Error: {key} must be an integer"
    return None


# 四个字符串字段（设计 §四）。schema 已声明 type: string，但网关不强制，所以
# 边界必须自己校验——这是 C1 的第一道防线，第二道是 task_store._as_str。
_STRING_FIELDS = ("content", "detail", "acceptance", "error")


def _check_string_fields(inp: dict) -> str | None:
    """模型边界的类型校验：本次调用里**提供了的**字符串字段必须是 str。

    为什么不能只靠 from_dict 的 _as_str：_as_str 只能把坏值收窄成 ""，模型收到的
    仍是 ok:true —— 它以为自己写下了一段执行方案，实际什么都没存，而 S 与披露层
    都不会告诉它。这里给模型一个可恢复的信号（Error: detail must be a string）。
    非字符串的 detail（步骤数组）是完全可能的模型错误，而它一旦落盘就会让
    .strip() 在每请求路径上抛 AttributeError，被 model_caller 与 prompt_runtime
    两处宽 except 吞掉：推式与常驻两层永久静默关闭，且没有 agent 可见的恢复路径。

    Plan 2 的 outcome 与 Plan 3 的写端点都会加字段——这个模式（类型校验属于模型
    边界，不只属于反序列化）从现在起就是既定的。
    """
    for key in _STRING_FIELDS:
        value = inp.get(key)
        if value is not None and not isinstance(value, str):
            return f"Error: {key} must be a string"
    return None


# ---- detail / acceptance 空字段的软提醒 ----
#
# 冒烟证据（qwen3.6-27b，8 文件 Python 包）：14 次 task_list 调用建出 4 条任务、
# 全部正确推到 completed、49 个测试通过，但四条的 detail 与 acceptance 都是
# 0 字符 → memory_injection 0 次，推式披露层从未触发；而 Plan 2 的验收闸门按
# 「这条声明了 acceptance 吗」自缩放，未声明就永不上膛。整个特性架在这两个字段上，
# 模型却把清单当纯 checklist 用。
#
# 根因是没有任何东西推回来：只带 content 的 add 与填写完整的 add 返回一模一样的
# ok:true。系统提示与工具 description 都只是**描述**这两个字段（89cb0d7 刚补过一轮
# 指引，模型跟上了「何时建清单」、没跟上「字段要填什么」），所以零代价可省的字段
# 就会被省。
# 这里给「省略」加一个即时、可行动的代价——放在工具结果里而不是提示词里，因为它在
# 调用返回后立刻被读到，那正是模型还能补写的时刻。
#
# 刻意是提醒而不是闸门（与验收闸门同为软警告的裁定同源）：硬要求会换来一个为了
# 过关编出来的 acceptance，而很多合法任务确实没有可验证命令；也会与「3 步以上就建
# 清单」的触发条件打架——简单清单被拒之门外比清单缺字段更糟。
#
# 体积自律：这两段文本会随 tool_result_msg 落进对话历史、按 token 付费，所以
# 只写「缺什么 + 为什么要紧（一个从句）+ 现在怎么补」，不重述工具 description。

_FIELD_WHY = {
    "detail": "detail 会在该条成为焦点时自动注入，可能是你届时眼前唯一的方案，须按读者没有其他上下文来写",
    "acceptance": "acceptance 是证明这条已完成的那一条命令，后续验收闸门按它是否声明来启用",
}


def _missing_spec_fields(item: TaskItem) -> list[str]:
    """该条尚未填写的方案字段（顺序固定：detail 在前）。"""
    missing = []
    if not item.detail.strip():
        missing.append("detail")
    if not item.acceptance.strip():
        missing.append("acceptance")
    return missing


def _add_hint(item: TaskItem) -> str | None:
    """add 之后的补写提醒；两个字段都已填则返回 None（做对了就不啰嗦）。

    只点名真正缺的字段，补写调用里也只带那些字段：模型填了 detail 却仍被念一遍
    detail，提醒就退化成了每次都出现的噪声，很快会被当背景读掉。
    """
    missing = _missing_spec_fields(item)
    if not missing:
        return None
    fix = ", ".join(f"{key}=..." for key in missing)
    return (
        f"未填 {'、'.join(missing)}："
        + "；".join(_FIELD_WHY[key] for key in missing)
        + f"。现在就补：task_list update(id={item.id}, {fix})"
    )


def _start_hint(item: TaskItem) -> str | None:
    """转入 in_progress 而 detail 仍空时的提醒 —— 比 add 那条更短更硬。

    只提 detail，不提 acceptance：一次流转只提醒当下最要紧的那一个字段。detail
    是这条任务唯一的方案来源，而此刻正是它还能被用上的最后时刻（焦点条已定，
    披露层马上要读它）；acceptance 到标 completed 之前都还来得及补。
    """
    if item.detail.strip():
        return None
    return (
        f"#{item.id} 已标 in_progress 而 detail 仍为空：这是动手前最后一次补写机会，"
        f"任务进行中不会再有方案注入。补写：task_list update(id={item.id}, detail=...)"
    )


def handle_task_list(
    session_id: str,
    inp: dict,
    current_seq: int | None = None,
    evidence_fn: Callable[[int | None], bool] | None = None,
) -> str:
    """处理 task_list 工具调用。

    current_seq: 承载本次 tool_calls 的 assistant 消息的 seq，由 dispatcher
    传入（dispatcher 拿的是 agent_loop 每个模型响应捕获一次、沿工具执行链
    传下来的值，不是可变的 agent.last_usage_seq）。store 不 import session
    （会引入循环依赖并破坏可测试性），所以 seq 只能由调用方给。

    evidence_fn: 软验收闸门的证据查询，`since_seq -> bool`（Plan 2 Task 3）。
    刻意是 callable 而不是 session：闸门要读事件日志，而 task_tools / task_store
    都不能 import session（循环依赖 + 可测试性），所以由 dispatcher 传一个对
    `agent.session` 的闭包进来（agents/tools/task_gate.has_successful_shell_since）。
    None = 闸门静默不启用，不报错也不警告。dispatcher 只在**这个 agent 有可能产出
    证据**时才给闭包（task_gate.can_produce_evidence，按工具集判）：拿不到证据工具
    的 agent 永远满足不了判据，对它上膛就是一台保证假阳性的机器。
    探针抛异常时的失败方向是「跳过警告」而不是「当成没有证据」——内部故障不能
    翻译成对模型的假指控（见 update 分支里的 try/except）。

    所有成功路径只返回纯 JSON，不拼披露文本：披露的单一注入路径是
    ensure_focus_detail_visible（Task 9）。工具层拿不到自己那条
    tool_result_msg 的 seq（handler 返回之后才落盘），若在这里也注入一份，
    无法记账 detail_origin_seq，下一轮会被重复注入。

    同理，_add_hint / _start_hint 的软提醒与验收闸门的 warning 都是 JSON 里的
    一个键（`hint` / `warning`），**不是**拼在 JSON 之后的散文：拼尾巴会让
    tool_result 既不是纯 JSON 又搭披露的便车
    （test_every_operation_returns_pure_json 与
    test_tool_result_does_not_carry_disclosure 是这条性质的门禁）。
    """
    # `.get("operation", "")` 的默认值只覆盖**键缺席**；键在场而值为 null 时拿到
    # 的是 None（冒烟里那 1 次畸形调用就是这种）。两条路都不在 _KEYS_BY_OPERATION
    # 里，于是 ignored 为空、五个分支全部落空，收敛到末尾同一句 clean error ——
    # 不是 KeyError，也不是被 dispatcher 宽 except 包出来的 traceback 噪声。
    operation = inp.get("operation", "")
    ignored = _ignored_keys(operation, inp)

    if operation == "add":
        type_error = _check_string_fields(inp)
        if type_error:
            return type_error
        id_error = _id_error(inp, "after_id")
        if id_error:
            return id_error
        content = (inp.get("content") or "").strip()
        if not content:
            return "Error: content is required for add operation"
        item = add_task(
            session_id,
            content,
            detail=inp.get("detail") or "",
            acceptance=inp.get("acceptance") or "",
            after_id=_coerce_id(inp.get("after_id")),
            # 承载这次 tool_calls 的 assistant seq：add(detail=...) 不记账就会在
            # 下一次模型调用时注入一份与 tool_calls 参数逐字相同的 detail（I1）。
            current_seq=current_seq,
        )
        # 提醒读的是**落盘后的条目**，不是入参：模型这次调用里填了的字段自然不在
        # missing 里，也就不会被重复念一遍（`detail="   "` 这类空白值仍算缺）。
        return _ok({"action": "added", "task": summary_dict(item)}, ignored,
                   hint=_add_hint(item))

    if operation == "update":
        # 先于「id is required」判：模型发了 id 只是发坏了，回它「required」是与事实
        # 矛盾的说法。after_id 同理——坏掉的 after_id 此前会让一次被请求的移动静默不
        # 发生，还回 ok:true。
        id_error = _id_error(inp, "id") or _id_error(inp, "after_id")
        if id_error:
            return id_error
        task_id = _coerce_id(inp.get("id"))
        if task_id is None:
            return "Error: id is required for update operation"
        type_error = _check_string_fields(inp)
        if type_error:
            return type_error
        status = inp.get("status")
        if status is not None and status not in VALID_STATUSES:
            return f"Error: invalid status '{status}'. Valid: {sorted(VALID_STATUSES)}"
        # 闸门与 _start_hint 要的都是「**真的发生了**这次流转」，所以两个目标状态都
        # 先读一眼旧状态。只在本次请求写这两个状态时才读：多出来的这次 store 读落在
        # update 分支上，不在每请求路径上（推式披露与常驻摘要已经各读一次了）。
        prev_status = None
        if status in (TASK_STATUS_COMPLETED, TASK_STATUS_IN_PROGRESS):
            prev_status = next(
                (t.status for t in list_tasks(session_id) if t.id == task_id), None)
        item = update_task(
            session_id,
            task_id,
            status=status,
            content=inp.get("content"),
            detail=inp.get("detail"),
            acceptance=inp.get("acceptance"),
            error=inp.get("error"),
            after_id=_coerce_id(inp.get("after_id")),
            current_seq=current_seq,
        )
        if item is None:
            return f"Error: task with id {task_id} not found"
        # 「一次流转一次提醒」需要**两个**条件，此前只有第一个：
        # - 本次请求写的新状态是 in_progress（不是 item.status）——否则一条已经
        #   in_progress 的任务后续每次改 content/after_id 都会被念一遍；
        # - 且这确实是一次流转（旧状态不是 in_progress）——否则模型读到提醒、去改别的
        #   字段、原样再发一次 status:"in_progress"，就收到逐字相同的提醒。
        # 第二条与下面验收闸门的 `prev_status != TASK_STATUS_COMPLETED` 同源同理由：
        # 重复的噪声正是训练模型忽略这个机制的东西。重开（in_progress → pending →
        # in_progress）后再转入是全新的时刻，那时照常提醒。
        hint = (
            _start_hint(item)
            if status == TASK_STATUS_IN_PROGRESS
            and prev_status != TASK_STATUS_IN_PROGRESS
            else None
        )
        # 软验收闸门（Plan 2 Task 3）：警告，不拒绝——状态照常变成 completed。
        # 上膛要四条同时成立：本次请求把状态改成 completed、**且这确实是一次流转**
        # （旧状态不是 completed）、这条声明了 acceptance（自缩放：没声明判据的任务
        # 不受约束）、调用方给了 evidence_fn（None = 闸门静默不启用）。
        # 「确实是一次流转」与上面 hint 的口径同源，理由也同一条：最可能的重复场景
        # 恰恰最伤——模型读到警告、认定自己确实验证过、原样再发一次
        # status:"completed"，于是收到逐字相同的警告。重复的噪声正是训练模型忽略
        # 这个机制的东西。重开（completed → in_progress）后再标完成是全新的验收
        # 时刻，那时闸门照常上膛。
        # 证据来自事件日志而不是模型自报；区间起点是 started_seq，为 None（跳过
        # in_progress 直接标 completed）时退化为全量扫描——宽松方向，宁漏勿误。
        warning = ""
        if (
            status == TASK_STATUS_COMPLETED
            and prev_status != TASK_STATUS_COMPLETED
            and item.acceptance.strip()
            and evidence_fn is not None
        ):
            try:
                has_evidence = bool(evidence_fn(item.started_seq))
            except Exception as exc:
                # 失败方向刻意**不是**「没有证据」：那会把一次内部故障翻译成对模型
                # 的假指控，而假指控是这套设计唯一禁止的结果（误报训练模型忽略警告）。
                # 也不让异常外逃——写入在探针之前就已落盘，外逃会让 dispatcher 的宽
                # except 给模型一个「其实已经成功了」的报错。于是「警告，绝不拒绝」
                # 在这里是结构性保证，不靠 evidence_fn 恰好不抛。留痕，不静默吞。
                print_error(
                    f"[acceptance_gate] evidence probe failed, "
                    f"skipping warning for task {task_id}: {exc!r}")
            else:
                if not has_evidence:
                    warning = build_acceptance_warning(item)
        payload = {"action": "updated", "task": summary_dict(item)}
        if warning:
            payload["warning"] = warning
        return _ok(payload, ignored, hint=hint)

    if operation == "remove":
        id_error = _id_error(inp, "id")
        if id_error:
            return id_error
        task_id = _coerce_id(inp.get("id"))
        if task_id is None:
            return "Error: id is required for remove operation"
        if not remove_task(session_id, task_id):
            return f"Error: task with id {task_id} not found"
        return _ok({"action": "removed", "id": task_id}, ignored)

    if operation == "list":
        tasks = list_tasks(session_id)
        focus = find_focus(tasks)
        done = sum(1 for t in tasks if t.status == TASK_STATUS_COMPLETED)
        return _ok({
            "count": len(tasks),
            "completed": done,
            "focus_id": focus.id if focus else None,
            "tasks": [summary_dict(t) for t in tasks],
            "note": "摘要视图，不含 detail。用 get 取单条完整方案。",
        }, ignored)

    if operation == "get":
        id_error = _id_error(inp, "id")
        if id_error:
            return id_error
        task_id = _coerce_id(inp.get("id"))
        if task_id is None:
            return "Error: id is required for get operation"
        for item in list_tasks(session_id):
            if item.id == task_id:
                return _ok({"task": full_dict(item)}, ignored)
        return f"Error: task with id {task_id} not found"

    return (
        f"Error: unknown operation '{operation}'. "
        "Valid: add, update, remove, list, get"
    )
