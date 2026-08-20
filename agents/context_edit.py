"""上下文可视化与局部删除（/context、/ctx del、/ctx keep）。

核心约束：删除消息必须保持工具调用配对完整性。
- OpenAI：assistant 消息的 tool_calls 必须与后续 role="tool" 且
  tool_call_id 匹配的消息成对存在；
- Anthropic：assistant 消息里的 tool_use block 必须与紧邻的 user 消息里的
  tool_result block 成对存在。

因此删除以"组"为单位：任何一条消息所属的配对组被整体删除，
保证删除后消息历史仍然合法。
"""

from __future__ import annotations

from typing import Any


def _msg_chars(msg: dict) -> int:
    """估算一条消息内容的字符数。"""
    content = msg.get("content")
    if content is None:
        # OpenAI 的 tool_calls-only assistant 消息 content 可能为 None。
        if msg.get("tool_calls"):
            return sum(len(str(tc.get("function", {}).get("arguments", ""))) for tc in msg["tool_calls"])
        return 0
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        total = 0
        for block in content:
            if isinstance(block, dict):
                total += len(str(block.get("text") or block.get("input") or block.get("content") or ""))
        return total
    return len(str(content))


def _msg_label(msg: dict) -> str:
    """给消息生成一个简短标签（工具名/摘要）。"""
    role = msg.get("role", "?")
    content = msg.get("content")

    # OpenAI tool_calls
    if msg.get("tool_calls"):
        names = [tc.get("function", {}).get("name", "?") for tc in msg["tool_calls"] if isinstance(tc, dict)]
        return f"tool_calls: {', '.join(names)}"
    if role == "tool":
        return f"tool_result({msg.get('tool_call_id', '?')[:12]})"

    if isinstance(content, list):
        parts = []
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "tool_use":
                parts.append(f"tool_use: {block.get('name', '?')}")
            elif btype == "tool_result":
                parts.append("tool_result")
            elif btype == "text":
                parts.append("text")
            elif btype == "thinking":
                parts.append("thinking")
        return ", ".join(parts) or "(blocks)"

    text = str(content or "")
    return text[:60].replace("\n", " ")


def describe_messages(messages: list[dict], use_openai: bool) -> list[dict]:
    """生成上下文表格行：index / role / label / chars。"""
    rows = []
    for i, msg in enumerate(messages):
        role = msg.get("role", "?")
        # OpenAI 第 0 条是 system prompt。
        if use_openai and i == 0 and role == "system":
            label = "(system prompt)"
        else:
            label = _msg_label(msg)
        rows.append({
            "index": i,
            "role": role,
            "label": label,
            "chars": _msg_chars(msg),
        })
    return rows


# ─── 配对组计算 ─────────────────────────────────────────────

def _openai_group(messages: list[dict], index: int) -> set[int]:
    """OpenAI：找出 index 所属的 tool_calls/tool 配对组。"""
    group = {index}
    msg = messages[index]

    if msg.get("role") == "assistant" and msg.get("tool_calls"):
        ids = {tc.get("id") for tc in msg["tool_calls"] if isinstance(tc, dict) and tc.get("id")}
        for j in range(index + 1, len(messages)):
            nxt = messages[j]
            if nxt.get("role") == "tool" and nxt.get("tool_call_id") in ids:
                group.add(j)
            elif nxt.get("role") == "assistant":
                break  # 下一个 assistant 消息，组结束

    elif msg.get("role") == "tool":
        target_id = msg.get("tool_call_id")
        # 向前找发起该 tool_call 的 assistant 消息。
        owner = None
        for j in range(index - 1, -1, -1):
            cand = messages[j]
            if cand.get("role") == "assistant" and cand.get("tool_calls"):
                if any(tc.get("id") == target_id for tc in cand["tool_calls"] if isinstance(tc, dict)):
                    owner = j
                    break
            if cand.get("role") == "user":
                break
        if owner is not None:
            group |= _openai_group(messages, owner)

    return group


def _anthropic_group(messages: list[dict], index: int) -> set[int]:
    """Anthropic：assistant(tool_use) 与紧邻 user(tool_result) 成组。"""
    group = {index}
    msg = messages[index]
    content = msg.get("content")

    def has_tool_use(m: dict) -> bool:
        c = m.get("content")
        return isinstance(c, list) and any(
            isinstance(b, dict) and b.get("type") == "tool_use" for b in c
        )

    def has_tool_result(m: dict) -> bool:
        c = m.get("content")
        return isinstance(c, list) and any(
            isinstance(b, dict) and b.get("type") == "tool_result" for b in c
        )

    if msg.get("role") == "assistant" and has_tool_use(msg):
        if index + 1 < len(messages) and has_tool_result(messages[index + 1]):
            group.add(index + 1)
    elif has_tool_result(msg):
        if index - 1 >= 0 and messages[index - 1].get("role") == "assistant" and has_tool_use(messages[index - 1]):
            group.add(index - 1)

    return group


def message_group(messages: list[dict], index: int, use_openai: bool) -> set[int]:
    if not (0 <= index < len(messages)):
        return set()
    return _openai_group(messages, index) if use_openai else _anthropic_group(messages, index)


def delete_message_group(messages: list[dict], index: int, use_openai: bool) -> tuple[list[dict], int]:
    """删除 index 所属配对组，返回 (新消息列表, 删除条数)。

    保护规则：
    - OpenAI 的 index 0（system prompt）不可删除；
    - 删除后若开头出现连续的同角色消息不做合并（API 可接受），
      但 Anthropic 侧调用方应随后跑 _normalize_anthropic_messages。
    """
    if not (0 <= index < len(messages)):
        return list(messages), 0
    if use_openai and index == 0 and messages[0].get("role") == "system":
        return list(messages), 0

    group = message_group(messages, index, use_openai)
    if not group:
        return list(messages), 0
    kept = [m for i, m in enumerate(messages) if i not in group]
    return kept, len(group)


def keep_only_groups(messages: list[dict], indexes: list[int], use_openai: bool) -> tuple[list[dict], int]:
    """只保留指定 index 所属的组（+ OpenAI system prompt），其余全删。"""
    keep: set[int] = set()
    for idx in indexes:
        keep |= message_group(messages, idx, use_openai)
    if use_openai and messages and messages[0].get("role") == "system":
        keep.add(0)
    kept = [m for i, m in enumerate(messages) if i in keep]
    return kept, len(messages) - len(kept)
