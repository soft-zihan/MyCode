"""ACE 式可逆上下文存储（arXiv 2606.31564v1 的最小可行实现）。

现有压缩（snip / microcompact / budget）是【有损】的：工具结果被替换成
占位符后原文永久丢失，后续若需要只能重新执行工具。

ContextStore 把压缩变成【可逆】的：
- 每次 snip/clear 之前，原文（raw）无损存入 store，键为 tool_use_id /
  tool_call_id；
- 占位符里带上键名，模型可用 context_restore 工具随时取回原文；
- /ctx del 手动删除的消息组会把对应条目标记为 dropped（持久 drop，
  不再自动参与恢复）；
- store 随 session 序列化，--resume 后依然可恢复。

raw + abstract 双层：abstract 是确定性头尾预览（零延迟、无 LLM 依赖），
raw 是完整原文。后续可扩展为 LLM 生成的语义摘要与按调用编排
（raw/abs/drop elasticizer），当前版本先保证"无损可逆"这一核心性质。
"""

from __future__ import annotations

from typing import Any

# 占位符前缀：snip/clear 检查用前缀匹配而非全等，
# 因为占位符现在携带每条内容唯一的 restore key。
SNIPPED_PREFIX = "[Content snipped"
CLEARED_PREFIX = "[Old result cleared"

ABSTRACT_PREVIEW_CHARS = 400  # abstract 预览长度（头尾各一半）


def make_abstract(content: str) -> str:
    """生成确定性的 abstract：头尾预览 + 长度信息。"""
    if len(content) <= ABSTRACT_PREVIEW_CHARS:
        return content
    half = ABSTRACT_PREVIEW_CHARS // 2
    return (
        content[:half]
        + f"\n[... {len(content) - ABSTRACT_PREVIEW_CHARS} chars omitted, use context_restore to recover ...]\n"
        + content[-half:]
    )


def is_compressed_placeholder(text: str) -> bool:
    """判断一段文本是否是 snip/clear 占位符。"""
    return isinstance(text, str) and (
        text.startswith(SNIPPED_PREFIX) or text.startswith(CLEARED_PREFIX)
    )


def snipped_placeholder(key: str, abstract: str = "") -> str:
    """生成带 restore key 的 snip 占位符。"""
    body = f"\nAbstract:\n{abstract}" if abstract else ""
    return (
        f"{SNIPPED_PREFIX} - original content stored reversibly. "
        f"Use the context_restore tool with key '{key}' to recover it.]{body}"
    )


def cleared_placeholder(key: str) -> str:
    return (
        f"{CLEARED_PREFIX} - stored reversibly. "
        f"Use the context_restore tool with key '{key}' to recover it.]"
    )


class ContextStore:
    """raw + abstract 双层无损存储。"""

    def __init__(self) -> None:
        # key -> {"raw": str, "abstract": str, "dropped": bool}
        self._entries: dict[str, dict[str, Any]] = {}

    def store(self, key: str, raw: str, abstract: str | None = None) -> None:
        """无损存入原文；重复键保留第一次的内容（幂等）。"""
        if key in self._entries:
            return
        self._entries[key] = {
            "raw": raw,
            "abstract": abstract if abstract is not None else make_abstract(raw),
            "dropped": False,
        }

    def has(self, key: str) -> bool:
        return key in self._entries

    def get_raw(self, key: str) -> str | None:
        entry = self._entries.get(key)
        if entry is None or entry["dropped"]:
            return None
        return entry["raw"]

    def get_abstract(self, key: str) -> str | None:
        entry = self._entries.get(key)
        if entry is None or entry["dropped"]:
            return None
        return entry["abstract"]

    def mark_dropped(self, key: str) -> None:
        """/ctx del 持久 drop：条目保留但不再可恢复。"""
        if key in self._entries:
            self._entries[key]["dropped"] = True

    def is_dropped(self, key: str) -> bool:
        entry = self._entries.get(key)
        return bool(entry and entry["dropped"])

    def available_keys(self) -> list[str]:
        return [k for k, v in self._entries.items() if not v["dropped"]]

    def __len__(self) -> int:
        return len(self._entries)

    # ── 序列化 ──────────────────────────────────────────────

    def to_dict(self) -> dict[str, Any]:
        return {"entries": self._entries}

    def restore_state(self, data: dict[str, Any]) -> None:
        entries = data.get("entries") or {}
        self._entries = {
            k: v for k, v in entries.items()
            if isinstance(v, dict) and "raw" in v
        }
