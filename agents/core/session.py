#!/usr/bin/env python3
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from pathlib import Path

from typing import Any
import json
import os
import time


def atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(content)
    tmp.rename(path)


def atomic_write_json(path: Path, data: Any, indent: int = 2) -> None:
    atomic_write_text(path, json.dumps(data, indent=indent, default=str))


def session_to_restore_dict(session: dict) -> dict:
    """Extract the 5 keys needed to restore a session into a standardized dict."""
    return {
        "openaiMessages": session.get("openaiMessages"),
        "foldedSessionMemories": session.get("foldedSessionMemories"),
        "checkpointStore": session.get("checkpointStore"),
        "turnBoundaries": session.get("turnBoundaries"),
        "contextStore": session.get("contextStore"),
    }


# ============================================================
# Tree-based Session Storage
# ============================================================


@dataclass
class SessionEntry:
    """Session 条目（树形结构节点）。
    
    Attributes:
        id: 条目 ID
        parent_id: 父条目 ID
        role: 角色 ("user" | "assistant" | "tool" | "system")
        content: 内容
        metadata: 元数据
            - tool_calls: list[{call_id, name, input}]
            - tool_results: list[{call_id, name, result, status}]
            - injected: bool (是否为 steering/follow-up 注入)
            - turn: int (轮次号)
    """
    
    id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    parent_id: str | None = None
    role: str = ""
    content: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    
    def to_dict(self) -> dict[str, Any]:
        """转换为字典。"""
        return {
            "id": self.id,
            "parent_id": self.parent_id,
            "role": self.role,
            "content": self.content,
            "metadata": self.metadata,
        }
    
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SessionEntry:
        """从字典创建。"""
        return cls(
            id=data.get("id", str(uuid.uuid4())[:8]),
            parent_id=data.get("parent_id"),
            role=data.get("role", ""),
            content=data.get("content", ""),
            metadata=data.get("metadata", {}),
        )


class Session:
    """树形 Session 存储。
    
    支持：
    - 添加条目到当前分支
    - 从任意条目创建分支（fork）
    - 获取当前分支的消息列表
    - 持久化到文件
    """
    
    def __init__(self, session_id: str | None = None) -> None:
        self.id = session_id or str(uuid.uuid4())
        self.entries: dict[str, SessionEntry] = {}
        self.children: dict[str | None, list[str]] = {}  # parent_id -> [entry_ids]
        self.current_branch: list[str] = []  # 当前分支的 entry_id 列表
        self.title: str | None = None
        self.summary: str | None = None
    
    def add_entry(self, entry: SessionEntry) -> str:
        """添加 entry 到当前分支。
        
        Args:
            entry: 要添加的条目
        
        Returns:
            str: 条目 ID
        """
        entry.parent_id = self.current_branch[-1] if self.current_branch else None
        self.entries[entry.id] = entry
        self.children.setdefault(entry.parent_id, []).append(entry.id)
        self.current_branch.append(entry.id)
        return entry.id
    
    def fork(self, entry_id: str) -> Session:
        """从指定 entry 创建新分支。
        
        Args:
            entry_id: 分支点条目 ID
        
        Returns:
            Session: 新的 Session 实例
        """
        new_session = Session()
        path = self._trace_path(entry_id)
        for eid in path:
            entry = self.entries[eid]
            new_entry = SessionEntry(
                id=entry.id,
                parent_id=entry.parent_id,
                role=entry.role,
                content=entry.content,
                metadata=entry.metadata.copy(),
            )
            new_session.entries[new_entry.id] = new_entry
            new_session.children.setdefault(new_entry.parent_id, []).append(new_entry.id)
            new_session.current_branch.append(new_entry.id)
        return new_session
    
    def get_messages(self) -> list[dict[str, Any]]:
        """获取当前分支的消息列表（用于 LLM 调用）。
        
        Returns:
            list[dict]: 消息列表
        """
        return [
            {"role": self.entries[eid].role, "content": self.entries[eid].content}
            for eid in self.current_branch
            if eid in self.entries
        ]
    
    def get_entries(self) -> list[SessionEntry]:
        """获取当前分支的所有条目。
        
        Returns:
            list[SessionEntry]: 条目列表
        """
        return [self.entries[eid] for eid in self.current_branch if eid in self.entries]
    
    def _trace_path(self, entry_id: str) -> list[str]:
        """从根到 entry_id 的路径。
        
        Args:
            entry_id: 目标条目 ID
        
        Returns:
            list[str]: 路径上的条目 ID 列表
        """
        path = []
        current = entry_id
        while current:
            path.append(current)
            if current not in self.entries:
                break
            current = self.entries[current].parent_id
        return list(reversed(path))
    
    def get_branch_point(self) -> str | None:
        """获取当前分支的分支点（最后一个有兄弟节点的条目）。
        
        Returns:
            str | None: 分支点条目 ID
        """
        for parent_id, child_ids in self.children.items():
            if len(child_ids) > 1:
                return child_ids[-2] if len(child_ids) >= 2 else None
        return None
    
    def list_branches(self) -> list[list[str]]:
        """列出所有分支。
        
        Returns:
            list[list[str]]: 分支列表，每个分支是条目 ID 列表
        """
        branches = []
        for parent_id, child_ids in self.children.items():
            if len(child_ids) > 1:
                for child_id in child_ids:
                    branch = self._trace_path(child_id)
                    branches.append(branch)
        if not branches:
            branches.append(self.current_branch.copy())
        return branches
    
    def save(self) -> None:
        """持久化到文件。"""
        data = {
            "id": self.id,
            "title": self.title,
            "summary": self.summary,
            "entries": [e.to_dict() for e in self.entries.values()],
            "current_branch": self.current_branch,
            "children": {str(k): v for k, v in self.children.items()},
        }
        save_session(self.id, data)
    
    @classmethod
    def load(cls, session_id: str) -> Session | None:
        """从文件加载。
        
        Args:
            session_id: Session ID
        
        Returns:
            Session | None: Session 实例
        """
        data = load_session(session_id)
        if data is None:
            return None
        
        session = cls(session_id)
        session.title = data.get("title")
        session.summary = data.get("summary")
        
        for entry_data in data.get("entries", []):
            entry = SessionEntry.from_dict(entry_data)
            session.entries[entry.id] = entry
        
        session.current_branch = data.get("current_branch", [])
        
        children_data = data.get("children", {})
        for parent_id, child_ids in children_data.items():
            key = None if parent_id == "null" else parent_id
            session.children[key] = child_ids
        
        return session
    
    def compact(self, summary: str) -> None:
        """压缩会话，保留摘要。
        
        Args:
            summary: 压缩后的摘要
        """
        if len(self.current_branch) <= 2:
            return
        
        # 保留 system 和第一条用户消息
        keep_entries = []
        for eid in self.current_branch[:2]:
            if eid in self.entries:
                keep_entries.append(eid)
        
        # 添加摘要条目
        summary_entry = SessionEntry(
            role="user",
            content=f"[Session compacted]\n\n{summary}",
            metadata={"compacted": True},
        )
        keep_entries.append(summary_entry.id)
        self.entries[summary_entry.id] = summary_entry
        
        # 更新当前分支
        self.current_branch = keep_entries


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
    atomic_write_json(session_dir() / f"{session_id}.json", data)


def save_folded_session_memory(session_id: str, record: dict[str, Any]) -> None:
    d = get_project_session_dir()
    line = json.dumps(record, ensure_ascii=False, default=str)
    with (d / f"{session_id}.folded-memory.jsonl").open("a", encoding="utf-8") as f:
        f.write(line + "\n")
    atomic_write_json(d / f"{session_id}.folded-memory.latest.json", record)


def load_session(session_id: str) -> dict[str, Any] | None:
    path = session_dir() / f"{session_id}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def save_session_meta(session_id: str, meta: dict[str, Any]) -> bool:
    path = session_dir() / f"{session_id}.json"
    if not path.exists():
        return False
    try:
        data = json.loads(path.read_text())
        if "metadata" not in data:
            data["metadata"] = {"id": session_id}
        data["metadata"].update(meta)
        atomic_write_json(path, data)
        return True
    except Exception:
        return False


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
