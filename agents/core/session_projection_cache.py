"""Session projection cache - persistent checkpoint for fast cold reads.

DeepSeek 对齐：
- 投影单元注册（title, updatedAt, cwd 等）
- 持久化 checkpoint 到存储
- 冷读时从 checkpoint + tail 重建，避免全量 replay
- 写入节流：turn/end 时强制写入，其他时候按 count/interval 节流
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .session import session_dir


@dataclass
class ProjectionCheckpoint:
    """投影缓存 checkpoint。
    
    Attributes:
        session_id: Session ID
        seq: Checkpoint 时的最后一个事件 seq
        projections: 投影值字典
        created_at: 创建时间
    """
    session_id: str
    seq: int
    projections: dict[str, Any]
    created_at: float = field(default_factory=time.time)
    
    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "seq": self.seq,
            "projections": self.projections,
            "created_at": self.created_at,
        }
    
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ProjectionCheckpoint:
        return cls(
            session_id=data["session_id"],
            seq=data["seq"],
            projections=data.get("projections", {}),
            created_at=data.get("created_at", time.time()),
        )


# 投影单元定义
# 每个投影单元是一个 fold：init() + apply(state, event) -> state
ProjectionUnit = dict[str, Any]  # {key, init, apply}


class ProjectionRegistry:
    """投影单元注册表。
    
    管理所有注册的投影单元，每个单元是一个纯同步 fold。
    """
    
    def __init__(self):
        self._units: dict[str, Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]] = {}
        self._init_values: dict[str, Any] = {}
    
    def register(
        self,
        key: str,
        init: Callable[[], Any],
        apply: Callable[[Any, dict[str, Any]], Any],
    ) -> None:
        """注册投影单元。
        
        Args:
            key: 投影键名
            init: 初始化函数，返回初始值
            apply: 折叠函数，接收 (state, event) 返回新 state
        """
        self._units[key] = apply
        self._init_values[key] = init()
    
    def init_state(self) -> dict[str, Any]:
        """返回初始投影状态。"""
        return {k: v for k, v in self._init_values.items()}
    
    def apply_event(self, state: dict[str, Any], event: dict[str, Any]) -> dict[str, Any]:
        """应用事件到投影状态。
        
        Args:
            state: 当前投影状态
            event: 事件
        
        Returns:
            新的投影状态
        """
        new_state = dict(state)
        for key, apply_fn in self._units.items():
            try:
                new_state[key] = apply_fn(state.get(key), event)
            except Exception:
                pass  # 投影失败不影响主流程
        return new_state
    
    def restore(
        self,
        checkpoint: ProjectionCheckpoint,
        tail_events: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """从 checkpoint + tail 重建投影状态。
        
        Args:
            checkpoint: Checkpoint
            tail_events: Checkpoint 之后的事件
        
        Returns:
            重建的投影状态
        """
        state = dict(checkpoint.projections)
        for event in tail_events:
            state = self.apply_event(state, event)
        return state


# 全局注册表
_projection_registry = ProjectionRegistry()


def register_projection(
    key: str,
    init: Callable[[], Any],
    apply: Callable[[Any, dict[str, Any]], Any],
) -> None:
    """注册投影单元。"""
    _projection_registry.register(key, init, apply)


def get_projection_registry() -> ProjectionRegistry:
    """获取全局投影注册表。"""
    return _projection_registry


# 注册默认投影单元
def _init_title() -> str | None:
    return None


def _apply_title(state: str | None, event: dict[str, Any]) -> str | None:
    if event.get("type") == "session/title":
        return event.get("title")
    if event.get("type") == "turn/end" and event.get("name"):
        return event.get("name")
    return state


def _init_updated_at() -> int:
    return 0


def _apply_updated_at(state: int, event: dict[str, Any]) -> int:
    event_time = event.get("time", 0)
    if event_time > state:
        return event_time
    return state


def _init_cwd() -> str | None:
    return None


def _apply_cwd(state: str | None, event: dict[str, Any]) -> str | None:
    if event.get("type") == "session/created" and event.get("cwd"):
        return event.get("cwd")
    return state


def _init_running() -> bool:
    return False


def _apply_running(state: bool, event: dict[str, Any]) -> bool:
    event_type = event.get("type")
    if event_type == "turn/start":
        return True
    if event_type == "turn/end":
        return False
    return state


# 注册默认投影
register_projection("title", _init_title, _apply_title)
register_projection("updated_at", _init_updated_at, _apply_updated_at)
register_projection("cwd", _init_cwd, _apply_cwd)
register_projection("running", _init_running, _apply_running)


class ProjectionCache:
    """投影缓存管理器。
    
    持久化 checkpoint 到 JSON 文件，支持：
    - 写入节流：turn/end 时强制写入，其他时候按 count/interval 节流
    - 冷读优化：从 checkpoint + tail 重建，避免全量 replay
    """
    
    def __init__(self, cache_dir: Path | None = None):
        if cache_dir is None:
            cache_dir = session_dir()
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        
        # 写入节流状态
        self._pending_writes: dict[str, int] = {}  # session_id -> event count since last write
        self._last_write_time: dict[str, float] = {}  # session_id -> last write timestamp
        
        # 节流配置
        self.write_count_threshold = 10  # 每 10 个事件写入一次
        self.write_interval_threshold = 5.0  # 每 5 秒写入一次
    
    def _cache_path(self, session_id: str) -> Path:
        return self.cache_dir / f"{session_id}.projcache.json"
    
    def load_checkpoint(self, session_id: str) -> ProjectionCheckpoint | None:
        """加载 checkpoint。"""
        path = self._cache_path(session_id)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text())
            return ProjectionCheckpoint.from_dict(data)
        except Exception:
            return None
    
    def save_checkpoint(self, checkpoint: ProjectionCheckpoint) -> None:
        """保存 checkpoint。"""
        path = self._cache_path(session_id := checkpoint.session_id)
        try:
            path.write_text(json.dumps(checkpoint.to_dict(), indent=2, default=str))
            self._last_write_time[session_id] = time.time()
            self._pending_writes[session_id] = 0
        except Exception:
            pass  # 写入失败不影响主流程
    
    def should_write(self, session_id: str) -> bool:
        """检查是否应该写入 checkpoint（节流判断）。"""
        count = self._pending_writes.get(session_id, 0)
        if count >= self.write_count_threshold:
            return True
        
        last_time = self._last_write_time.get(session_id, 0)
        if time.time() - last_time >= self.write_interval_threshold:
            return True
        
        return False
    
    def record_event(self, session_id: str) -> None:
        """记录一个事件（用于节流计数）。"""
        self._pending_writes[session_id] = self._pending_writes.get(session_id, 0) + 1
    
    def force_write(self, session_id: str) -> None:
        """强制写入（turn/end 时调用）。"""
        self._pending_writes[session_id] = self.write_count_threshold
    
    def delete_checkpoint(self, session_id: str) -> None:
        """删除 checkpoint。"""
        path = self._cache_path(session_id)
        if path.exists():
            path.unlink()
        self._pending_writes.pop(session_id, None)
        self._last_write_time.pop(session_id, None)


# 全局缓存实例
_projection_cache: ProjectionCache | None = None


def get_projection_cache() -> ProjectionCache:
    """获取全局投影缓存实例。"""
    global _projection_cache
    if _projection_cache is None:
        _projection_cache = ProjectionCache()
    return _projection_cache


def build_projections_from_events(
    events: list[dict[str, Any]],
) -> dict[str, Any]:
    """从事件列表构建投影状态。
    
    Args:
        events: 事件列表
    
    Returns:
        投影状态字典
    """
    registry = get_projection_registry()
    state = registry.init_state()
    for event in events:
        state = registry.apply_event(state, event)
    return state


def restore_projections(
    session_id: str,
    all_events: list[dict[str, Any]],
) -> dict[str, Any]:
    """恢复投影状态（从 checkpoint + tail 或全量 replay）。
    
    Args:
        session_id: Session ID
        all_events: 所有事件
    
    Returns:
        投影状态字典
    """
    cache = get_projection_cache()
    registry = get_projection_registry()
    
    # 尝试从 checkpoint 恢复
    checkpoint = cache.load_checkpoint(session_id)
    if checkpoint is not None:
        # 找到 checkpoint 之后的事件
        tail_events = [e for e in all_events if e.get("seq", 0) > checkpoint.seq]
        
        # 从 checkpoint + tail 重建
        state = registry.restore(checkpoint, tail_events)
        
        # 更新 checkpoint（如果有新事件）
        if tail_events:
            last_event = tail_events[-1]
            new_checkpoint = ProjectionCheckpoint(
                session_id=session_id,
                seq=last_event.get("seq", 0),
                projections=state,
            )
            cache.save_checkpoint(new_checkpoint)
        
        return state
    
    # 没有 checkpoint，全量 replay
    state = build_projections_from_events(all_events)
    
    # 保存 checkpoint
    if all_events:
        last_event = all_events[-1]
        checkpoint = ProjectionCheckpoint(
            session_id=session_id,
            seq=last_event.get("seq", 0),
            projections=state,
        )
        cache.save_checkpoint(checkpoint)
    
    return state
