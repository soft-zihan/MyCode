"""派生期补齐无结果的 tool_call，并按工具的重放策略给出诚实措辞。

真实流程是 `assistant_message`（带 tool_calls）先落盘、工具后执行，所以进程被硬杀
（kill -9 / OOM / Pod 驱逐）会在事件流里留下永远等不到 `tool_result_msg` 的调用。
OpenAI 兼容接口对「assistant.tool_calls 后面没有配对的 role=tool」这个形状直接 400，
于是这个会话从此续不下去——而崩溃恢复只合成 `turn/end` 闭合 turn，不管工具调用。

**为什么在派生期补、而不回写事件流**（对齐 codex 的 `ensure_call_outputs_present`，
它同样只在 prompt 组装期合成、不回写 rollout）：写回会与「工具其实还在跑」竞态——
abort 只设标志，真正的收尾要等当前 step 结束，这期间补一条结果进去，等真结果落盘
就成了同 id 两条。派生期合成天然幂等，而且事件流保持 append-only 的诚实：
日志里没发生的事，不往日志里写。

**措辞按重放策略分两种**（策略表见 agents/tools/registry.py 的 REPLAY_SAFE_TOOLS）：
unsafe 说「可能已部分执行，先查现场」，safe 说「同参数收敛，可直接重跑」。
两种都**不自动重放**——我们没有 Pi Durable 那种「意图先提交、phase 可重入」的
任务状态机，硬要自动重跑就是把重复副作用交给运气。把事实告诉模型、让它决定，
是 Vercel durable-harness 对非 safe 工具的同一处理方式。

子 Agent 是特例：`sub_agent/start` 早就把子会话 id 落盘了，只是从没投影进 LLM 消息，
所以硬杀之后模型连「该续跑哪个」都不知道，人只能自己去事件流里翻。这里把未收尾的
子会话 id 拼进合成结果，模型就能自己带 session_id 续跑（照 opencode 的
`settleStaleToolCalls` 做的——它把子会话 id 拼进 Tool.Failed 文本，一处同时解决
400 与 id 丢失）。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

from agents.tools.registry import REPLAY_SAFE_TOOLS

REPLAY_SAFE = "safe"
REPLAY_UNSAFE = "unsafe"

_UNSAFE_TEXT = (
    "工具 {name} 的结果未记录：进程在工具执行期间中断了。"
    "该操作不可安全重跑，可能已产生部分副作用。"
    "请先核实现场（例如 git status、读回文件、查一下那条命令有没有生效）再决定要不要重试，"
    "不要按原参数直接重跑。"
)
_SAFE_TEXT = (
    "工具 {name} 的结果未记录：进程在工具执行期间中断了。"
    "该操作可安全重跑（同参数收敛到同一状态、无额外外部副作用），需要时直接重试即可。"
)
_SUBAGENT_SUFFIX = (
    "这是一次子 Agent 派发，事件流里这些子会话在中断时还没收尾：{ids}。"
    "要接着干就用 agent 工具带 session_id=<上面的 id> 续跑，别重新派发一个。"
)

_SUB_START = "sub_agent/start"
_SUB_ENDED = ("sub_agent/end", "subagent/completed")


def replay_policy(tool_name: str) -> str:
    """这个工具按原参数重跑安全吗。没登记过的一律 unsafe（含所有 MCP 工具）。"""
    return REPLAY_SAFE if tool_name in REPLAY_SAFE_TOOLS else REPLAY_UNSAFE


def unterminated_sub_sessions(events: Iterable[dict[str, Any]]) -> list[str]:
    """从事件流派生「启动了但没收尾」的子会话 id（保持出现顺序、去重）。

    不建表、不查注册表：job_registry 是进程内的，硬杀之后本来就空了，
    唯一还在的地方就是事件流。
    """
    started: list[str] = []
    seen: set[str] = set()
    ended: set[str] = set()
    for event in events:
        sid = event.get("sub_session_id")
        if not sid:
            continue
        etype = event.get("type")
        if etype == _SUB_START:
            if sid not in seen:
                seen.add(sid)
                started.append(sid)
        elif etype in _SUB_ENDED:
            ended.add(sid)
    return [sid for sid in started if sid not in ended]


def _interrupted_text(tool_name: str, sub_session_ids: Sequence[str]) -> str:
    template = _SAFE_TEXT if replay_policy(tool_name) == REPLAY_SAFE else _UNSAFE_TEXT
    text = template.format(name=tool_name)
    if tool_name == "agent" and sub_session_ids:
        text += " " + _SUBAGENT_SUFFIX.format(ids="、".join(sub_session_ids))
    return text


def ensure_call_outputs_present(
    messages: list[dict[str, Any]],
    events: Iterable[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """给消息列表里每个无结果的 tool_call 补一条 role=tool，返回新列表。

    不改入参（那是 Session 的派生缓存）；已经有结果的调用不重复补，
    哪怕它的结果在历史里更靠后——同 id 出现两条 tool 消息，网关一样会拒。
    """
    if not messages:
        return []

    answered = {
        m.get("tool_call_id") for m in messages if m.get("role") == "tool" and m.get("tool_call_id")
    }
    dangling = [
        call
        for m in messages
        if m.get("role") == "assistant"
        for call in m.get("tool_calls") or []
        if call.get("id") and call["id"] not in answered
    ]
    if not dangling:
        return list(messages)

    # 只有真出现悬挂的子 Agent 派发时才去扫事件流，别让常规轮次白付这个开销
    needs_sub_ids = any(
        ((call.get("function") or {}).get("name") == "agent") for call in dangling
    )
    sub_ids = unterminated_sub_sessions(events or ()) if needs_sub_ids else []

    out: list[dict[str, Any]] = []
    for message in messages:
        out.append(message)
        if message.get("role") != "assistant" or not message.get("tool_calls"):
            continue
        for call in message["tool_calls"]:
            call_id = call.get("id")
            if not call_id or call_id in answered:
                continue
            name = (call.get("function") or {}).get("name") or "未知工具"
            out.append({
                "role": "tool",
                "tool_call_id": call_id,
                "content": _interrupted_text(name, sub_ids),
            })
            answered.add(call_id)
    return out
