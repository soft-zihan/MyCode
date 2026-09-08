#!/usr/bin/env python3
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from pathlib import Path

from typing import Any, Callable, Iterator
import json
import os
import time

from .session_backend import SessionBackend
from .session_backend_jsonl import JsonlSessionBackend


# 只推不持久化的事件类型（流式事件）
SSE_ONLY_TYPES = frozenset({
    "thinking", "text", "tool_call", "tool_result",
})


def atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(content)
    tmp.rename(path)


def atomic_write_json(path: Path, data: Any, indent: int = 2) -> None:
    atomic_write_text(path, json.dumps(data, indent=indent, default=str))


def session_to_restore_dict(session: dict) -> dict:
    """Extract the keys needed to restore a session from event log."""
    return {
        "events": session.get("events"),
        "contextStore": session.get("contextStore"),
    }


# Global backend instance
_backend: SessionBackend | None = None


def get_session_backend() -> SessionBackend:
    """Get the global session backend instance."""
    global _backend
    if _backend is None:
        backend_type = os.environ.get("MYCODE_SESSION_BACKEND", "jsonl").strip().lower()
        if backend_type == "sqlite":
            from .session_backend_sqlite import SqliteSessionBackend
            _backend = SqliteSessionBackend()
        else:
            _backend = JsonlSessionBackend()
    return _backend


def set_session_backend(backend: SessionBackend) -> None:
    """Set the global session backend instance (for testing)."""
    global _backend
    _backend = backend


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
    """Session 存储。
    
    支持：
    - 树形结构：添加条目到当前分支、分支（fork）
    - 事件日志：append/subscribe/replay（用于 SSE 和持久化）
    - 消息派生：get_messages_for_llm（从事件日志提取 LLM 消息历史）
    - 统一标记方案：删除、工具折叠、会话折叠都使用标记事件
    - 持久化到文件
    """
    
    def __init__(
        self,
        session_id: str | None = None,
        parent_session: str | None = None,
        origin: str | None = None,
        agent_type: str | None = None,
    ) -> None:
        self.id = session_id or uuid.uuid4().hex[:8]
        self.parent_session = parent_session  # 父 session ID（用于子智能体）
        self.origin = origin  # 来源标记：'sub_agent' 表示子智能体
        self.agent_type = agent_type  # 智能体类型（用于子智能体）
        self.entries: dict[str, SessionEntry] = {}
        self.children: dict[str | None, list[str]] = {}  # parent_id -> [entry_ids]
        self.current_branch: list[str] = []  # 当前分支的 entry_id 列表
        self.title: str | None = None
        self.summary: str | None = None
        self.system_prompt: str | None = None  # 系统提示词（单独存储）
        
        self._log: list[dict[str, Any]] = []
        self._subscribers: set[Callable[[dict], None]] = set()
        
        # Initialize projections with default values from registry
        try:
            from .session_projection_cache import get_projection_registry
            self._projections: dict[str, Any] = get_projection_registry().init_state()
        except Exception:
            self._projections: dict[str, Any] = {}  # Fallback if registry not available
    
    @property
    def seq(self) -> int:
        return len(self._log)
    
    @property
    def events(self) -> tuple[dict[str, Any], ...]:
        return tuple(self._log)
    
    @property
    def projections(self) -> dict[str, Any]:
        """获取投影缓存（title, updatedAt, cwd, running）。"""
        return self._projections
    
    def append(self, type: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
        """唯一写入入口。自动区分持久化和只推送。
        
        流式事件（thinking, text, tool_call, tool_result, stats）只推送不持久化。
        聚合事件（user_message, assistant_message, tool_result_msg 等）持久化 + 推送。
        
        纯同步方法，asyncio 下天然原子。
        """
        event = {
            "type": type,
            "time": int(time.time() * 1000),
            "session_id": self.id,
            **(data or {}),
        }
        
        # 调试日志
        if type in ['turn/start', 'user_message', 'assistant_message']:
            print(f"[DEBUG] Session.append: type={type}, self.id={self.id}, event.session_id={event.get('session_id')}, id(self)={id(self)}")
        
        if type in SSE_ONLY_TYPES:
            # 流式事件：只推送，不持久化
            for sub in list(self._subscribers):
                try:
                    sub(event)
                except Exception:
                    pass
        else:
            # 聚合事件：持久化 + 推送
            event["seq"] = len(self._log)
            self._log.append(event)
            
            # Persist to backend (skip for sub-agents)
            if self.origin != "sub_agent":
                try:
                    backend = get_session_backend()
                    backend.append(self.id, event)
                except Exception:
                    pass  # Fallback: in-memory only
            
            # Update projections
            try:
                from .session_projection_cache import get_projection_registry, get_projection_cache
                registry = get_projection_registry()
                self._projections = registry.apply_event(self._projections, event)
                
                # Check if we should write checkpoint
                cache = get_projection_cache()
                cache.record_event(self.id)
                
                # Force write on turn/end
                if type == "turn/end":
                    cache.force_write(self.id)
                
                if cache.should_write(self.id):
                    from .session_projection_cache import ProjectionCheckpoint
                    checkpoint = ProjectionCheckpoint(
                        session_id=self.id,
                        seq=event["seq"],
                        projections=self._projections,
                    )
                    cache.save_checkpoint(checkpoint)
            except Exception:
                pass  # Projection cache is optional
            
            for sub in list(self._subscribers):
                try:
                    sub(event)
                except Exception:
                    pass
        
        # Broadcast to WebSocket subscribers
        try:
            # 使用与 main.py 相同的导入路径
            from routers.websocket import broadcast_event
            broadcast_event(event, target_session_id=self.id)
            
            # 如果是子智能体，也广播给父 session 的订阅者
            if self.origin == "sub_agent" and self.parent_session:
                broadcast_event(event, target_session_id=self.parent_session)
        except (ImportError, TypeError):
            pass  # WebSocket module not available or type evaluation error
        
        return event
    
    def subscribe(self, callback: Callable[[dict], None]) -> Callable[[], None]:
        """订阅事件流。返回取消订阅函数。"""
        self._subscribers.add(callback)
        return lambda: self._subscribers.discard(callback)
    
    def replay(self, since_seq: int = 0) -> Iterator[dict[str, Any]]:
        """从指定 seq 开始重放事件。"""
        for event in self._log:
            if event["seq"] >= since_seq:
                yield event
    
    def get_messages_for_llm(self) -> list[dict[str, Any]]:
        """从事件日志构建 LLM 消息历史。
        
        使用统一标记方案：
        1. 收集所有 deleted_seqs（从 context/events_deleted 事件）
        2. 收集所有 snipped（从 context/tool_snipped 事件）
        3. 遍历事件，跳过 deleted_seqs 中的 seq
        4. 遇到 tool_result_msg 时，检查是否被 snipped，如果是则用占位符替换
        5. 遇到 context/session_folded 时，插入摘要消息
        """
        messages = []
        
        # 1. 收集所有标记事件
        deleted_seqs = set()
        snipped_map = {}  # seq -> key
        fold_summaries = []  # [(seq, summary), ...]
        
        for event in self._log:
            t = event.get("type")
            if t == "context/events_deleted":
                deleted_seqs.update(event.get("deleted_seqs", []))
            elif t == "context/tool_snipped":
                for item in event.get("snipped", []):
                    snipped_map[item["seq"]] = item["key"]
            elif t == "context/session_folded":
                fold_summaries.append((event.get("seq"), event.get("summary", "")))
        
        # 2. 添加系统提示词
        if self.system_prompt:
            messages.append({"role": "system", "content": self.system_prompt})
        
        # 3. 插入最早的折叠摘要（如果有）
        if fold_summaries:
            earliest_summary = fold_summaries[0][1]
            messages.append({"role": "assistant", "content": earliest_summary})
        
        # 4. 遍历事件，构建消息
        for event in self._log:
            seq = event.get("seq")
            t = event.get("type")
            
            # 跳过被删除的事件
            if seq in deleted_seqs:
                continue
            
            # 跳过标记事件本身
            if t in ("context/events_deleted", "context/tool_snipped", "context/session_folded"):
                continue
            
            # 处理消息事件
            if t == "user_message":
                messages.append({"role": "user", "content": event["content"]})
            elif t == "assistant_message":
                msg: dict[str, Any] = {"role": "assistant", "content": event.get("content", "")}
                if event.get("thinking"):
                    msg["thinking"] = event["thinking"]
                if event.get("tool_calls"):
                    msg["tool_calls"] = event["tool_calls"]
                messages.append(msg)
            elif t == "tool_result_msg":
                # 检查是否被 snipped
                if seq in snipped_map:
                    from .context_store import snipped_placeholder
                    key = snipped_map[seq]
                    # 尝试从 ContextStore 获取 abstract
                    abstract = ""
                    try:
                        from agents.agent import _current_agent
                        if _current_agent and hasattr(_current_agent, '_context_store'):
                            abstract = _current_agent._context_store.get_abstract(key)
                    except Exception:
                        pass
                    content = snipped_placeholder(key, abstract)
                else:
                    content = event["content"]
                
                messages.append({
                    "role": "tool",
                    "tool_call_id": event["call_id"],
                    "content": content,
                })
        
        return messages
    
    def mark_deleted(self, seqs: list[int], trigger: str = "manual") -> None:
        """标记指定 seq 的事件为已删除。
        
        Args:
            seqs: 要删除的事件 seq 列表
            trigger: 触发方式 "manual" | "auto"
        
        行为：
        1. 写入 "context/events_deleted" 标记事件（append-only）
        2. 广播删除通知给前端
        
        原子性保证：
        - 只追加新事件，不修改已有事件
        - 标记事件持久化后，重启时从标记事件恢复 deleted_seqs 状态
        """
        if not seqs:
            return
        
        self.append("context/events_deleted", {
            "deleted_seqs": seqs,
            "trigger": trigger,
        })
    
    def mark_tool_snipped(self, snipped: list[dict], trigger: str = "auto") -> None:
        """标记工具结果被替换为占位符。
        
        Args:
            snipped: [{"seq": 3, "key": "snip:call_001"}, ...]
            trigger: 触发方式
        
        行为：
        1. 写入 "context/tool_snipped" 标记事件（append-only）
        2. 广播通知给前端
        """
        if not snipped:
            return
        
        self.append("context/tool_snipped", {
            "snipped": snipped,
            "trigger": trigger,
        })
    
    # ── 三阶段恢复 ──
    
    def _find_snapshot_at_seq(self, target_seq: int) -> dict | None:
        """找到指定 seq 之前的最近的 snapshot/end 事件。"""
        for event in reversed(self._log[:target_seq]):
            if event.get("type") == "snapshot/end":
                return event
        return None
    
    def _capture_current_file_states(self) -> list[dict]:
        """捕获当前文件状态（路径 + hash）。"""
        import hashlib
        from pathlib import Path
        
        files = []
        try:
            cwd = Path.cwd()
            ignore_dirs = {".git", ".venv", "node_modules", "__pycache__", ".mypy_cache", ".pytest_cache"}
            
            for file_path in cwd.rglob("*"):
                if not file_path.is_file():
                    continue
                if any(part.startswith(".") or part in ignore_dirs for part in file_path.parts):
                    continue
                try:
                    content = file_path.read_bytes()
                    files.append({
                        "path": str(file_path.relative_to(cwd)),
                        "hash": hashlib.md5(content).hexdigest()[:8],
                        "size": len(content),
                    })
                except (OSError, PermissionError):
                    pass
        except Exception:
            pass
        return files
    
    def stage_revert(self, target_seq: int) -> dict:
        """Stage：计算恢复计划，预览变更。
        
        Returns:
            dict: 恢复计划，包含 current_snapshot, restore_plan, files_to_change
        """
        # 1. 捕获当前状态（用于取消）
        current_snapshot = self._capture_current_file_states()
        
        # 2. 从事件日志读取目标快照
        target_snapshot = self._find_snapshot_at_seq(target_seq)
        if not target_snapshot:
            return {
                "error": f"No snapshot found before seq {target_seq}",
                "current_snapshot": current_snapshot,
                "restore_plan": {},
                "files_to_change": [],
            }
        
        target_files = target_snapshot.get("files", [])
        
        # 3. 计算恢复计划
        restore_plan = {}
        files_to_change = []
        for file_info in target_files:
            path = file_info["path"]
            current_file = next((f for f in current_snapshot if f["path"] == path), None)
            if not current_file or current_file["hash"] != file_info["hash"]:
                files_to_change.append(path)
                restore_plan[path] = file_info
        
        # 4. 返回预览（不执行）
        return {
            "current_snapshot": current_snapshot,
            "target_seq": target_seq,
            "restore_plan": restore_plan,
            "files_to_change": files_to_change,
        }
    
    def clear_revert(self, current_snapshot: list[dict]) -> dict:
        """Clear：取消恢复，恢复到原始状态。
        
        Args:
            current_snapshot: stage_revert 返回的 current_snapshot
        
        Returns:
            dict: 恢复结果
        """
        # 恢复到原始状态
        restored_files = []
        for file_info in current_snapshot:
            path = Path(file_info["path"])
            if path.exists():
                # 文件存在，检查是否需要恢复
                import hashlib
                try:
                    current_hash = hashlib.md5(path.read_bytes()).hexdigest()[:8]
                    if current_hash != file_info["hash"]:
                        # 文件已被修改，需要恢复
                        # 注意：这里假设我们有文件内容的备份
                        # 实际实现中，我们需要从 snapshot 事件中获取文件内容
                        restored_files.append(file_info["path"])
                except (OSError, PermissionError):
                    pass
        
        # 记录取消事件
        self.append("revert/clear", {
            "restored_files": len(restored_files),
        })
        
        return {
            "restored_files": restored_files,
            "status": "cleared",
        }
    
    def commit_revert(self, target_seq: int) -> dict:
        """Commit：确认恢复。
        
        Args:
            target_seq: 目标 seq
        
        Returns:
            dict: 恢复结果
        """
        # 1. 从事件日志读取目标快照
        target_snapshot = self._find_snapshot_at_seq(target_seq)
        if not target_snapshot:
            return {
                "error": f"No snapshot found before seq {target_seq}",
                "status": "failed",
            }
        
        target_files = target_snapshot.get("files", [])
        
        # 2. 执行恢复（这里只是记录，实际文件恢复需要实现）
        restored_files = []
        for file_info in target_files:
            restored_files.append(file_info["path"])
        
        # 3. 截断事件日志
        self._log = self._log[:target_seq]
        
        # 4. 记录恢复事件
        self.append("revert/commit", {
            "target_seq": target_seq,
            "restored_files": len(restored_files),
        })
        
        return {
            "restored_files": restored_files,
            "status": "committed",
        }
    
    @classmethod
    def load_from_jsonl(cls, session_id: str) -> Session | None:
        """从后端加载事件日志，验证 seq 连续性，应用崩溃恢复和投影缓存。"""
        # 尝试从 JSON 文件读取元数据
        json_path = session_dir() / f"{session_id}.json"
        metadata = {}
        if json_path.exists():
            try:
                metadata = json.loads(json_path.read_text())
            except Exception:
                pass
        
        session = cls(
            session_id=session_id,
            parent_session=metadata.get("parent_session"),
            origin=metadata.get("origin"),
            agent_type=metadata.get("agent_type"),
        )
        
        # Load events from backend
        try:
            backend = get_session_backend()
            events = backend.load_all_events(session_id)
        except Exception:
            # Fallback: try loading from JSONL file directly
            events = []
            path = cls._jsonl_path(session_id)
            if path.exists():
                for line in path.read_text(encoding="utf-8").strip().splitlines():
                    if not line.strip():
                        continue
                    try:
                        event = json.loads(line)
                        events.append(event)
                    except json.JSONDecodeError:
                        break
        
        if not events:
            return None
        
        # Apply crash recovery (torn tail detection + auto-repair)
        from .session_crash_recovery import validate_and_repair_events
        events = validate_and_repair_events(events, session_id)
        
        # Validate seq continuity and load
        expected_seq = 0
        for event in events:
            if event.get("seq") != expected_seq:
                break
            session._log.append(event)
            expected_seq += 1
        
        # Build projections from events (with checkpoint optimization)
        try:
            from .session_projection_cache import restore_projections
            session._projections = restore_projections(session_id, session._log)
        except Exception:
            pass  # Projection cache is optional
        
        return session if session._log else None
    
    @staticmethod
    def _jsonl_path(session_id: str) -> Path:
        return session_dir() / f"{session_id}.events.jsonl"
    
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
            "parent_session": self.parent_session,
            "origin": self.origin,
            "agent_type": self.agent_type,
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
        
        session = cls(
            session_id=session_id,
            parent_session=data.get("parent_session"),
            origin=data.get("origin"),
            agent_type=data.get("agent_type"),
        )
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
    """会话存储目录。默认 ~/.mycode/sessions/，
    可用 MYCODE_SESSION_DIR 环境变量重定向（测试用，避免污染真实目录）。"""
    override = os.environ.get("MYCODE_SESSION_DIR", "").strip()
    if override:
        return Path(override)
    return Path.home() / ".mycode" / "sessions"


def _ensure_dir() -> None:
    session_dir().mkdir(parents=True, exist_ok=True)


def get_project_session_dir() -> Path:
    d = Path.cwd() / ".mycode" / "sessions"
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
    """列出所有 session，包括磁盘上的和内存中的。"""
    _ensure_dir()
    results = []
    seen_ids = set()
    
    # 1. 先从内存中获取活跃的 session
    try:
        from agents.session_manager import get_session_manager
        manager = get_session_manager()
        for session in manager.active_sessions():
            metadata = {
                "id": session.id,
                "name": session.title or session.id,
                "cwd": session.projections.get("cwd", ""),
                "startTime": session.projections.get("updated_at") or "",
                "model": "",
                "parent_session": session.parent_session,
                "origin": session.origin,
                "agent_type": session.agent_type,
            }
            results.append(metadata)
            seen_ids.add(session.id)
    except Exception:
        pass  # session_manager 可能不可用
    
    # 2. 再从磁盘读取 session（去重）
    for f in session_dir().glob("*.json"):
        # 跳过 projcache 文件
        if f.name.endswith(".projcache.json"):
            continue
        try:
            data = json.loads(f.read_text())
            if "metadata" in data:
                metadata = data["metadata"]
                session_id = metadata.get("id")
                if session_id and session_id not in seen_ids:
                    # 从 projcache 文件读取 updated_at
                    projcache_path = session_dir() / f"{session_id}.projcache.json"
                    if projcache_path.exists():
                        try:
                            projcache = json.loads(projcache_path.read_text())
                            updated_at = projcache.get("projections", {}).get("updated_at", 0)
                            if updated_at:
                                metadata["startTime"] = updated_at
                        except Exception:
                            pass
                    results.append(metadata)
                    seen_ids.add(session_id)
        except Exception:
            pass
    
    return results


def list_child_sessions(parent_session_id: str) -> list[dict[str, Any]]:
    """列出指定父 session 的所有子 session。
    
    Args:
        parent_session_id: 父 session ID
    
    Returns:
        list[dict]: 子 session 元数据列表
    """
    _ensure_dir()
    results = []
    for f in session_dir().glob("*.json"):
        try:
            data = json.loads(f.read_text())
            # parent_session 在顶层，不在 metadata 中
            if data.get("parent_session") == parent_session_id:
                # 返回 metadata（如果存在）或基本信息
                if "metadata" in data:
                    results.append(data["metadata"])
                else:
                    results.append({
                        "id": data.get("id", f.stem),
                        "parent_session": data.get("parent_session"),
                        "origin": data.get("origin"),
                        "agent_type": data.get("agent_type"),
                    })
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
