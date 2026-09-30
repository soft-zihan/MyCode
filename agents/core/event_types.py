"""事件公开面清单（U8 事件治理，对齐 v2 packages/schema/src/event-manifest.ts）。

单源机器可查：哪些事件允许推上公开通道（WS 广播），哪些是内部持久化/审计事件。
新事件默认不公开——必须显式加入本清单，防止内部事件泄漏成前端隐性依赖。

MANIFEST_VERSION：公开面 schema 的版本锚。公开事件的**不兼容** shape 变更时 bump
（兼容变更——新增可选字段——不 bump）。消费端（Web/TUI）可据此判断重放兼容性。
"""

from __future__ import annotations

MANIFEST_VERSION = 1

# ---------------------------------------------------------------------------
# 公开面：前端实时渲染消费（消费方引用见各组注释）
# ---------------------------------------------------------------------------

PUBLIC_EVENT_TYPES = frozenset({
    # 流式渲染（SSE_ONLY，不落盘；useChatNodes/useChat）
    "thinking", "text", "tool_call", "tool_result",
    # 聚合渲染（useChat/EventRouter/useChatNodes）
    "user_message", "error", "stats",
    "turn/start", "turn/end",
    "context/compacted", "tool_folded", "session_folded",
    # 会话生命周期（EventRouter/useChat）
    "session/created", "session/title", "session/plan_linked", "session/interrupted",
    # 交互请求（useChat 权限/提问对话框）
    "permission/request", "permission/mode_changed",
    "question/request", "question/resolved",
    # 状态面板（useChat todo/plan 投影）
    "task_list/updated", "plan/updated",
    # 子代理（useChatNodes 子代理节点 + 后台任务面板 + 完成通知卡）
    "sub_agent/start", "sub_agent/end", "subagent/completed",
    # eval 家族中唯一经 session.append 落盘的会话级事件（其余 eval/* 走前缀放行）
    "eval/task_metadata",
})

# 前缀放行：eval/* 事件族由 EvalPage 全量动态收集渲染（run/task/judge/trace 各阶段），
# 枚举会随评测演进频繁变化，按前缀放行（v2 event-manifest 对 durable 族同款处理）。
PUBLIC_EVENT_PREFIXES: tuple[str, ...] = ("eval/",)

# ---------------------------------------------------------------------------
# 内部事件：持久化/审计/投影，但不上公开通道
# ---------------------------------------------------------------------------

INTERNAL_EVENT_TYPES = frozenset({
    # LLM 消息聚合（历史渲染走 HTTP messages 投影，非实时流）
    "assistant_message", "tool_result_msg",
    # 步级审计
    "step/start", "step/end",
    # 会话归属元数据（子代理/eval 会话列表过滤的数据源）
    "session/meta",
    # 子代理续跑/取消审计
    "sub_agent/resume", "sub_agent/cancel",
    # steer 审计（前端乐观 UI，不消费审计事件）
    "steer/queued", "steer/delivered", "steer/dropped",
    # 结构操作（rewind 走 HTTP 响应；events_hidden 触发投影重烘焙）
    "rewind", "events_hidden",
    # 注入与守门审计（注入内容经 derive 进历史，不实时渲染）
    "memory_injection", "repeated_tool_calls",
    # 权限决议落盘（前端经 HTTP 响应关闭对话框）
    "permission/resolved",
    # turn 取消兜底 closer（正常路径前端消费 turn/end{reason}）
    "turn/cancel",
    # 崩溃恢复 seq 空洞占位（仅内存，永不广播）
    "seq_gap",
})

ALL_KNOWN_EVENT_TYPES = PUBLIC_EVENT_TYPES | INTERNAL_EVENT_TYPES


def is_public_event(event_type: str) -> bool:
    """事件是否允许推上公开通道（WS 广播）。"""
    if event_type in PUBLIC_EVENT_TYPES:
        return True
    return any(event_type.startswith(prefix) for prefix in PUBLIC_EVENT_PREFIXES)
