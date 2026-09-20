#!/usr/bin/env python3
from __future__ import annotations

import uuid
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


class Session:
    """Event-sourced Session 存储。

    - 事件日志是唯一数据源：append/subscribe/replay 驱动 SSE 与持久化
    - LLM 消息通过 get_messages_for_llm 从可见事件增量派生
    - 删除、工具折叠、会话折叠都使用标记事件，保持 append-only
    """
    
    def __init__(
        self,
        session_id: str | None = None,
        parent_session: str | None = None,
        origin: str | None = None,
        agent_type: str | None = None,
    ) -> None:
        self.id = session_id or uuid.uuid4().hex[:8]
        self.parent_session = parent_session
        self.origin = origin
        self.agent_type = agent_type
        self.summary: str | None = None
        self.system_prompt: str | None = None
        
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
        except Exception as e:
            print(f"[session] 投影注册表初始化失败，使用空投影: {e!r}")
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
                except Exception as e:
                    print(f"[session] 事件订阅者异常: type={type} err={e!r}")
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
                except Exception as e:
                    print(f"[session] backend.append 失败（事件仅在内存，存在丢失风险）: type={type} seq={event.get('seq')} err={e!r}")
            
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
            except Exception as e:
                print(f"[session] 投影缓存更新失败: type={type} err={e!r}")
            
            for sub in list(self._subscribers):
                try:
                    sub(event)
                except Exception as e:
                    print(f"[session] 事件订阅者异常: type={type} err={e!r}")
        
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
        return derive_messages_from_event(event)


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
    
    def truncate_events_to(self, keep_seq: int) -> None:
        """截断内存事件日志：保留 seq < keep_seq 的事件。

        与 backend.truncate(session_id, keep_seq) 的物理截断配套使用
        （见 agents/core/rewind_service.py）。截断后 _next_seq 重置为
        keep_seq，保证后续事件 seq 与 jsonl 连续；_surface_generation
        递增使消息投影全量重建。
        """
        self._log = [e for e in self._log if e.get("seq", 0) < keep_seq]
        self._visible_seqs = [s for s in self._visible_seqs if s < keep_seq]
        self._next_seq = keep_seq
        self._surface_generation += 1

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
                if rows.get("plan_slug") and rows["plan_slug"].get("val"):
                    metadata["plan_slug"] = rows["plan_slug"]["val"]
            except Exception as e:
                print(f"[session] projcache 读取失败 {projcache_path}: {e!r}")
        
        session = cls(session_id=session_id)
        
        session.cwd = metadata.get("cwd")
        session.plan_slug = metadata.get("plan_slug")
        
        backend = get_session_backend()
        events = backend.load_all_events(session_id)
        if not events:
            return None

        from .session_crash_recovery import validate_and_repair_events
        events = validate_and_repair_events(events, session_id)

        events_by_seq: dict[int, dict] = {}
        for event in events:
            seq = event.get("seq")
            if seq is not None and seq not in events_by_seq:
                events_by_seq[seq] = event

        max_seq = -1
        for seq in sorted(events_by_seq.keys()):
            session._log.append(events_by_seq[seq])
            max_seq = max(max_seq, seq)

        session._next_seq = max_seq + 1

        hidden_seqs = set()
        for event in session._log:
            if event.get("type") == "events_hidden":
                hidden_seqs.update(event.get("hidden_seqs", []))

        session._visible_seqs = [
            e["seq"] for e in session._log
            if e.get("seq") not in hidden_seqs and e.get("type") != "events_hidden"
        ]
        session._surface_generation = len(session._log)

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
        
        return session if session._log else None
    

def derive_messages_from_event(event: dict[str, Any]) -> list[dict[str, Any]]:
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
    except Exception as e:
        print(f"[session] session 元数据读取失败: {e!r}")
    
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
        except Exception as e:
            print(f"[session] projcache 解析失败: {e!r}")
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



