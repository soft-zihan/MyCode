
#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path

from typing import Any
import json
import os
import time


def session_dir() -> Path:
    """会话存储目录。默认 ~/.bear-code/sessions/，
    可用 BEAR_SESSION_DIR 环境变量重定向（测试用，避免污染真实目录）。"""
    override = os.environ.get("BEAR_SESSION_DIR", "").strip()
    if override:
        return Path(override)
    return Path.home() / ".bear-code" / "sessions"


def _ensure_dir() -> None:
    session_dir().mkdir(parents=True, exist_ok=True)


def get_project_session_dir() -> Path:
    d = Path.cwd() / ".bear" / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    return d


def save_session(session_id: str, data: dict[str, Any]) -> None:
    _ensure_dir()
    (session_dir() / f"{session_id}.json").write_text(json.dumps(data, indent=2, default=str))


def save_folded_session_memory(session_id: str, record: dict[str, Any]) -> None:
    d = get_project_session_dir()
    line = json.dumps(record, ensure_ascii=False, default=str)
    with (d / f"{session_id}.folded-memory.jsonl").open("a", encoding="utf-8") as f:
        f.write(line + "\n")
    (d / f"{session_id}.folded-memory.latest.json").write_text(
        json.dumps(record, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )


def load_session(session_id: str) -> dict[str, Any] | None:
    path = session_dir() / f"{session_id}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def list_sessions() -> list[dict[str, Any]]:
    _ensure_dir()
    results = []
    for f in session_dir().glob("*.json"):
        try:
            data = json.loads(f.read_text())
            if "metadata" in data:
                results.append(data["metadata"])
        except Exception:
            pass
    return results


def delete_session(session_id: str) -> bool:
    """删除指定会话文件。返回是否真的删除了。"""
    path = session_dir() / f"{session_id}.json"
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return False


def clean_sessions(keep_latest: int = 20, only_tmp: bool = False) -> int:
    """批量清理会话文件，返回删除数量。

    only_tmp=True 时只删 cwd 位于临时目录（pytest/tmp）的测试污染会话；
    否则按时间排序只保留最近 keep_latest 个。
    """
    sessions = list_sessions()
    sessions.sort(key=lambda s: s.get("startTime", ""), reverse=True)
    deleted = 0
    tmp_markers = ("/pytest-of-", "/tmp/", "/private/tmp/", "/var/folders/")
    if only_tmp:
        for s in sessions:
            cwd = s.get("cwd", "")
            if any(m in cwd for m in tmp_markers):
                if delete_session(s.get("id", "")):
                    deleted += 1
    else:
        for s in sessions[keep_latest:]:
            if delete_session(s.get("id", "")):
                deleted += 1
    return deleted


def get_latest_session_id() -> str | None:
    sessions = list_sessions()
    if not sessions:
        return None
    sessions.sort(key=lambda s: s.get("startTime", ""), reverse=True)
    return sessions[0].get("id")


# ─── Branch Summary (Phase 3.2) ───────────────────────────────────────────────


def generate_branch_summary(
    entries: list[dict[str, Any]],
    common_ancestor_id: str | None = None,
) -> dict[str, Any]:
    """生成分支摘要，保留离开分支时的经验。

    Args:
        entries: 当前分支的对话条目列表
        common_ancestor_id: 共同祖先的 entry id（摘要到此为止）

    Returns:
        branch_summary 条目，包含摘要信息
    """
    entries_to_summarize = []
    for entry in entries:
        if entry.get("id") == common_ancestor_id:
            break
        entries_to_summarize.append(entry)

    # 提取关键信息
    user_messages = []
    assistant_messages = []
    tool_calls = []

    for entry in entries_to_summarize:
        role = entry.get("role", "")
        content = entry.get("content", "")
        if role == "user":
            user_messages.append(content[:200] if isinstance(content, str) else str(content)[:200])
        elif role == "assistant":
            assistant_messages.append(content[:200] if isinstance(content, str) else str(content)[:200])

        # 提取工具调用
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    tool_calls.append({
                        "name": block.get("name", ""),
                        "input": str(block.get("input", ""))[:100],
                    })

    summary = {
        "type": "branch_summary",
        "timestamp": time.time(),
        "from_id": entries[0].get("id") if entries else None,
        "to_id": common_ancestor_id,
        "summary": {
            "user_turns": len(user_messages),
            "assistant_turns": len(assistant_messages),
            "tool_calls": len(tool_calls),
            "key_user_requests": user_messages[:3],  # 前 3 个用户请求
            "key_actions": assistant_messages[:3],    # 前 3 个助手回复
            "tools_used": list({tc["name"] for tc in tool_calls})[:10],  # 去重后的工具列表
        },
    }

    return summary


def save_branch_summary(session_id: str, branch_summary: dict[str, Any]) -> None:
    """保存分支摘要到会话文件。"""
    data = load_session(session_id)
    if data is None:
        return

    if "branch_summaries" not in data:
        data["branch_summaries"] = []

    data["branch_summaries"].append(branch_summary)
    save_session(session_id, data)


def load_branch_summaries(session_id: str) -> list[dict[str, Any]]:
    """加载会话的分支摘要列表。"""
    data = load_session(session_id)
    if data is None:
        return []
    return data.get("branch_summaries", [])


def format_branch_summary_for_injection(summaries: list[dict[str, Any]]) -> str:
    """格式化分支摘要，用于注入到新分支的上下文。"""
    if not summaries:
        return ""

    parts = ["<branch-history>"]
    parts.append("Previous branch activity (for context):")

    for i, s in enumerate(summaries[-3:]):  # 最近 3 个分支摘要
        summary = s.get("summary", {})
        parts.append(f"\n[Branch {i+1}]")
        parts.append(f"- User requests: {summary.get('user_turns', 0)} turns")
        parts.append(f"- Actions taken: {summary.get('assistant_turns', 0)} turns")
        parts.append(f"- Tools used: {', '.join(summary.get('tools_used', [])[:5])}")

        key_requests = summary.get("key_user_requests", [])
        if key_requests:
            parts.append(f"- Key requests: {key_requests[0][:100]}...")

    parts.append("\nUse this history to maintain continuity with previous branch work.")
    parts.append("</branch-history>")

    return "\n".join(parts)
