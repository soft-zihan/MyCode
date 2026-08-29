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


def estimate_tokens(text: str) -> int:
    """粗略估算 token 数（统一用 token 计数展示）。

    启发式：CJK 字符约 1 字符 ≈ 0.7 token，其余约 4 字符 ≈ 1 token。
    精确值只有 API 上报的 usage 才有，这里用于 /context 的per-message 展示。
    """
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    other = len(text) - cjk
    return int(cjk * 0.7 + other / 4)


def _msg_tokens(msg: dict) -> int:
    """估算一条消息内容的 token 数。"""
    content = msg.get("content")
    if content is None:
        if msg.get("tool_calls"):
            return estimate_tokens("".join(str(tc.get("function", {}).get("arguments", "")) for tc in msg["tool_calls"]))
        return 0
    if isinstance(content, str):
        return estimate_tokens(content)
    if isinstance(content, list):
        total = 0
        for block in content:
            if isinstance(block, dict):
                total += estimate_tokens(str(block.get("text") or block.get("input") or block.get("content") or ""))
        return total
    return estimate_tokens(str(content))


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
    """生成上下文表格行：index / role / label / chars / tokens。"""
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
            "tokens": _msg_tokens(msg),
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


def message_group(messages: list[dict], index: int, use_openai: bool = True) -> set[int]:
    if not (0 <= index < len(messages)):
        return set()
    return _openai_group(messages, index)


def delete_message_group(messages: list[dict], index: int, use_openai: bool = True) -> tuple[list[dict], int]:
    """删除 index 所属配对组，返回 (新消息列表, 删除条数)。

    保护规则：
    - OpenAI 的 index 0（system prompt）不可删除；
    - 删除后若开头出现连续的同角色消息不做合并（API 可接受）。
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


# ─── 批量索引表达式解析 ─────────────────────────────────────

def parse_index_spec(spec: str) -> list[int]:
    """解析批量索引表达式，如 "1,3,5~10"、"1 3 5-10"、"2,4~6 9"。

    支持逗号/空格混合分隔，`~` 或 `-` 表示闭区间范围。
    返回去重升序列表；非法 token 抛 ValueError。
    """
    indexes: set[int] = set()
    for tok in spec.replace(",", " ").split():
        tok = tok.strip()
        if not tok:
            continue
        sep = None
        if "~" in tok:
            sep = "~"
        elif "-" in tok and not tok.lstrip("-").isdigit():
            sep = "-"  # 仅当不是负数字面量时，"-" 才视为范围分隔符
        if sep is None:
            indexes.add(int(tok))  # 非法字符在此抛 ValueError
            continue
        lo_s, hi_s = tok.split(sep, 1)
        lo, hi = int(lo_s), int(hi_s)
        if lo > hi:
            lo, hi = hi, lo
        indexes.update(range(lo, hi + 1))
    return sorted(indexes)
