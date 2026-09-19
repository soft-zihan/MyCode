#!/usr/bin/env python3
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from pathlib import Path

from typing import Any, Callable, Iterator
import json
import os
import time

from agents.core.workspace import get_workspace

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
        self.summary: str | None = None
        self.system_prompt: str | None = None  # 系统提示词（单独存储）
        
        self._log: list[dict[str, Any]] = []
        self._subscribers: set[Callable[[dict], None]] = set()
        self._next_seq: int = 0  # 下一个事件的 seq（基于已加载的最大 seq + 1）
        
        # Surface 索引机制
        self._visible_seqs: list[int] = []  # 可见事件的索引
        self._surface_generation: int = 0  # Surface 变化计数
        
        # 增量派生状态（对齐 DeepSeek deriveMessages）
        self._derived_messages: list[dict[str, Any]] = []
        self._derived_node_count: int = 0
        self._derived_generation: int = -1
        
        # 身份属性（对齐 DeepSeek SessionHeader）
        self.created_at: int = int(time.time() * 1000)
        self.cwd: str | None = None
        self.is_seeded: bool = False
        self.inherited_event_count: int = 0
        
        # Initialize projections with default values from registry
        try:
            from .session_projection_cache import get_projection_registry
            self._projections: dict[str, Any] = get_projection_registry().init_state()
        except Exception:
            self._projections: dict[str, Any] = {}
        
        # ask_user 工具响应存储
        self.question_responses: dict[str, dict[str, Any]] = {}
        
        # Plan 系统关联
        self.plan_slug: str | None = None
        
        # 策略快照（用于持久化 Plan 执行时的策略版本）
        # 格式: {stage: {name, source, content_hash}}
        self.plan_strategy_snapshot: dict[str, dict[str, str]] = {}
    
    @property
    def seq(self) -> int:
        return self._next_seq
    
    @property
    def events(self) -> tuple[dict[str, Any], ...]:
        return tuple(self._log)
    
    @property
    def projections(self) -> dict[str, Any]:
        """获取投影缓存（title, updatedAt, cwd, running）。"""
        return self._projections
    
    @property
    def title(self) -> str | None:
        """Session 标题（单一数据源：事件流投影，由 session/title 事件驱动）。"""
        return self._projections.get("title")
    
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
            event["seq"] = self._next_seq
            self._next_seq += 1
            self._log.append(event)
            
            # 新事件默认可见（events_hidden 本身不加入索引）
            if type != "events_hidden":
                self._visible_seqs.append(event["seq"])
            self._surface_generation += 1
            
            # Persist to backend (skip for sub-agents)
            if self.origin != "sub_agent":
                try:
                    backend = get_session_backend()
                    backend.append(self.id, event)
                except Exception:
                    pass  # Fallback: in-memory only
            
            # Update projections
            try:
                from .session_projection_cache import get_projection_registry, get_projection_cache, ProjectionCheckpoint, CheckpointRow
                registry = get_projection_registry()
                self._projections = registry.apply_event(self._projections, event)
                
                if self.origin != "sub_agent":
                    cache = get_projection_cache()
                    cache.record_event(self.id)
                    
                    if type in ("turn/end", "session/title"):
                        cache.force_write(self.id)
                    
                    if cache.should_write(self.id):
                        rows = registry.checkpoint(self._projections, event["seq"])
                        checkpoint = ProjectionCheckpoint.from_session(self, rows)
                        cache.save_checkpoint(checkpoint)
            except Exception:
                pass
            
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
        """从事件日志构建 LLM 消息历史（增量派生）。
        
        对齐 DeepSeek 的 deriveMessages()：
        - generation 变化时重置（surface 结构变化）
        - 只处理新增的可见事件（增量）
        """
        _t0 = time.perf_counter()
        
        if self._derived_generation != self._surface_generation:
            self._derived_messages = []
            self._derived_node_count = 0
            self._derived_generation = self._surface_generation
            
            if self.system_prompt:
                self._derived_messages.append({
                    "role": "system",
                    "content": self.system_prompt,
                })
        
        new_visible = self._visible_seqs[self._derived_node_count:]
        for seq in new_visible:
            event = self._log[seq]
            msgs = self._derive_messages(event)
            self._derived_messages.extend(msgs)
        
        self._derived_node_count = len(self._visible_seqs)
        
        _elapsed = (time.perf_counter() - _t0) * 1000
        if _elapsed > 5 or len(new_visible) > 0:
            import sys
            print(
                f"[perf] get_messages_for_llm: {_elapsed:.2f}ms "
                f"(processed {len(new_visible)} new, total {len(self._derived_messages)} msgs)",
                file=sys.stderr,
            )
        
        return list(self._derived_messages)
    
    def _derive_messages(self, event: dict[str, Any]) -> list[dict[str, Any]]:
        """从单个事件派生 LLM 消息列表。"""
        t = event.get("type")
        
        if t == "tool_folded":
            return [
                {
                    "role": "tool",
                    "tool_call_id": abstract["call_id"],
                    "content": abstract["abstract"],
                }
                for abstract in event.get("abstracts", [])
            ]
        
        if t == "session_folded":
            return [{"role": "assistant", "content": event.get("summary", "")}]
        
        if t in ("user_message", "memory_injection"):
            return [{"role": "user", "content": event.get("content", "")}]
        
        if t == "assistant_message":
            msg: dict[str, Any] = {"role": "assistant", "content": event.get("content", "")}
            if event.get("thinking"):
                msg["thinking"] = event["thinking"]
            if event.get("tool_calls"):
                msg["tool_calls"] = event["tool_calls"]
            return [msg]
        
        if t == "tool_result_msg":
            return [{
                "role": "tool",
                "tool_call_id": event["call_id"],
                "content": event["content"],
            }]
        
        return []
    
    def hide_events(self, seqs: list[int]) -> None:
        """隐藏指定 seq 的事件。
        
        追加 events_hidden 事件，同时从 _visible_seqs 移除。
        这样保持 append-only 的纯洁性，同时支持 Surface 索引机制。
        """
        if not seqs:
            return
        
        # 追加 events_hidden 事件（会更新 _surface_generation）
        self.append("events_hidden", {
            "hidden_seqs": list(seqs),
        })
        
        # 从索引中移除
        seq_set = set(seqs)
        self._visible_seqs = [s for s in self._visible_seqs if s not in seq_set]
    
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

        cwd = get_workspace()
        files = []
        ignore_dirs = {".git", ".venv", "node_modules", "__pycache__", ".mypy_cache", ".pytest_cache"}
        try:
            for file_path in cwd.rglob("*"):
                try:
                    if not file_path.is_file():
                        continue
                    # 隐藏/忽略目录判断基于相对工作区的路径（工作区本身可能位于隐藏路径下）
                    rel = file_path.relative_to(cwd)
                    if any(part.startswith(".") or part in ignore_dirs for part in rel.parts):
                        continue
                    content = file_path.read_bytes()
                    files.append({
                        "path": str(rel),
                        "hash": hashlib.md5(content).hexdigest()[:8],
                        "size": len(content),
                    })
                except OSError:
                    continue
        except OSError as e:
            from agents.observability.trace import trace_event
            trace_event("snapshot.capture_failed", error=str(e), workspace=str(cwd))
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
        
        # 3. 截断事件列表
        self._log = self._log[:target_seq]
        
        # 同步更新 _visible_seqs
        self._visible_seqs = [s for s in self._visible_seqs if s < target_seq]
        self._surface_generation += 1
        
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
    def load_from_events(cls, session_id: str) -> Session | None:
        """从后端加载事件日志，验证 seq 连续性，应用崩溃恢复和投影缓存。
        
        支持 JSONL 和 SQLite 两种后端，通过 MYCODE_SESSION_BACKEND 环境变量切换。
        """
        # 从 projcache 读取元数据
        projcache_path = session_dir() / f"{session_id}.projcache.json"
        metadata = {}
        if projcache_path.exists():
            try:
                projcache = json.loads(projcache_path.read_text())
                rows = projcache.get("rows", {})
                if rows.get("cwd") and rows["cwd"].get("val"):
                    metadata["cwd"] = rows["cwd"]["val"]
                if rows.get("parent_session") and rows["parent_session"].get("val"):
                    metadata["parent_session"] = rows["parent_session"]["val"]
                if rows.get("origin") and rows["origin"].get("val"):
                    metadata["origin"] = rows["origin"]["val"]
                if rows.get("agent_type") and rows["agent_type"].get("val"):
                    metadata["agent_type"] = rows["agent_type"]["val"]
                if rows.get("plan_slug") and rows["plan_slug"].get("val"):
                    metadata["plan_slug"] = rows["plan_slug"]["val"]
            except Exception:
                pass
        
        session = cls(
            session_id=session_id,
            parent_session=metadata.get("parent_session"),
            origin=metadata.get("origin"),
            agent_type=metadata.get("agent_type"),
        )
        
        session.cwd = metadata.get("cwd")
        session.plan_slug = metadata.get("plan_slug")
        
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
        
        # Load all events, handling gaps and duplicates gracefully
        # Group events by seq, keeping the first occurrence of each seq
        events_by_seq: dict[int, dict] = {}
        for event in events:
            seq = event.get("seq")
            if seq is not None and seq not in events_by_seq:
                events_by_seq[seq] = event
        
        # Load events in seq order
        max_seq = -1
        for seq in sorted(events_by_seq.keys()):
            session._log.append(events_by_seq[seq])
            max_seq = max(max_seq, seq)
        
        # Set _next_seq to max_seq + 1 to prevent duplicate seq numbers
        session._next_seq = max_seq + 1
        
        # 重建 _visible_seqs
        hidden_seqs = set()
        for event in session._log:
            if event.get("type") == "events_hidden":
                hidden_seqs.update(event.get("hidden_seqs", []))
        
        session._visible_seqs = [
            e["seq"] for e in session._log
            if e.get("seq") not in hidden_seqs and e.get("type") != "events_hidden"
        ]
        session._surface_generation = len(session._log)  # 初始 generation
        
        # Build projections from events (with checkpoint optimization)
        try:
            from .session_projection_cache import restore_projections, SessionHeader
            header = SessionHeader(
                id=session.id,
                version=1,
                created_at=session.created_at,
                cwd=session.cwd,
                is_seeded=session.is_seeded,
                inherited_event_count=session.inherited_event_count,
            )
            session._projections = restore_projections(session_id, session._log, header)
        except Exception:
            pass
        
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
        
        # 复制 Surface 索引
        new_session._visible_seqs = self._visible_seqs.copy()
        new_session._surface_generation = self._surface_generation
        
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
    d = get_workspace() / ".mycode" / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    return d





def save_folded_session_memory(session_id: str, record: dict[str, Any]) -> None:
    d = get_project_session_dir()
    line = json.dumps(record, ensure_ascii=False, default=str)
    with (d / f"{session_id}.folded-memory.jsonl").open("a", encoding="utf-8") as f:
        f.write(line + "\n")
    atomic_write_json(d / f"{session_id}.folded-memory.latest.json", record)





def list_sessions() -> list[dict[str, Any]]:
    """列出所有 session，包括磁盘上的和内存中的。直接从 projcache 读取投影数据。"""
    import time as _time
    _t0 = _time.time()
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
                "plan_slug": session.plan_slug,
            }
            results.append(metadata)
            seen_ids.add(session.id)
    except Exception:
        pass
    
    # 2. 直接从 projcache 文件读取投影数据（不需要读 session 文件）
    import concurrent.futures
    
    def read_projcache(f):
        try:
            session_id = f.name.replace(".projcache.json", "")
            if session_id in seen_ids:
                return None
            projcache = json.loads(f.read_text())
            rows = projcache.get("rows", {})
            
            metadata = {
                "id": session_id,
                "name": session_id,
                "cwd": "",
                "startTime": "",
                "model": "",
                "parent_session": "",
                "origin": "",
                "agent_type": "",
                "plan_slug": None,
            }
            
            if rows.get("cwd") and rows["cwd"].get("val"):
                metadata["cwd"] = rows["cwd"]["val"]
            if rows.get("updated_at") and rows["updated_at"].get("val"):
                metadata["startTime"] = rows["updated_at"]["val"]
            if rows.get("title") and rows["title"].get("val"):
                metadata["name"] = rows["title"]["val"]
            if rows.get("plan_slug") and rows["plan_slug"].get("val"):
                metadata["plan_slug"] = rows["plan_slug"]["val"]
            
            return metadata
        except Exception:
            return None
    
    projcache_files = list(session_dir().glob("*.projcache.json"))
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(read_projcache, f): f for f in projcache_files}
        for future in concurrent.futures.as_completed(futures):
            result = future.result()
            if result:
                results.append(result)
                seen_ids.add(result.get("id"))
    
    _elapsed = _time.time() - _t0
    if _elapsed > 0.5:
        print(f"[PERF] list_sessions: {_elapsed:.2f}s for {len(results)} sessions")
    return results


def list_child_sessions(parent_session_id: str) -> list[dict[str, Any]]:
    """列出指定父 session 的所有子 session。从 projcache 读取。"""
    _ensure_dir()
    results = []
    for f in session_dir().glob("*.projcache.json"):
        try:
            projcache = json.loads(f.read_text())
            rows = projcache.get("rows", {})
            if rows.get("parent_session") and rows["parent_session"].get("val") == parent_session_id:
                session_id = f.name.replace(".projcache.json", "")
                metadata = {
                    "id": session_id,
                    "parent_session": parent_session_id,
                }
                if rows.get("title") and rows["title"].get("val"):
                    metadata["name"] = rows["title"]["val"]
                results.append(metadata)
        except Exception:
            pass
    return results


def delete_session(session_id: str) -> bool:
    """删除指定会话的全部文件（事件日志/投影缓存/折叠记忆）。返回是否真的删除了。"""
    d = session_dir()
    patterns = (
        f"{session_id}.events.jsonl",
        f"{session_id}.projcache.json",
        f"{session_id}.folded-memory.jsonl",
        f"{session_id}.folded-memory.latest.json",
    )
    deleted = False
    for name in patterns:
        try:
            (d / name).unlink()
            deleted = True
        except FileNotFoundError:
            continue
        except OSError:
            continue
    return deleted


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



