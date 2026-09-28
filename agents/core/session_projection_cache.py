"""Session projection cache - persistent checkpoint for fast cold reads.

DeepSeek 对齐：
- 投影单元注册（title, updatedAt, cwd 等）+ stateVersion
- 持久化 checkpoint 到存储（per-session, per-key rows）
- 冷读时从 checkpoint + tail 重建，版本/身份不匹配则丢弃
- 写入节流：turn/end 时强制写入，其他时候按 count/interval 节流
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .session import session_dir
from agents.config import DEFAULT_CONTEXT_WINDOW


FORMAT_VERSION = 1


@dataclass(frozen=True)
class SessionHeader:
    """Session 生命周期身份。

    checkpoint 绑定此身份，防止错误的 checkpoint 用于错误的 session。
    """
    id: str
    version: int
    created_at: int
    cwd: str | None
    is_seeded: bool = False
    inherited_event_count: int = 0


@dataclass
class CheckpointRow:
    """单个投影单元的 checkpoint 行。"""
    ver: int
    seq: int
    val: Any

    def to_dict(self) -> dict[str, Any]:
        return {"ver": self.ver, "seq": self.seq, "val": self.val}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CheckpointRow:
        return cls(ver=data["ver"], seq=data["seq"], val=data["val"])


@dataclass
class ProjectionCheckpoint:
    """投影缓存 checkpoint。

    包含身份字段和按 key 存储的行，版本不匹配时安全丢弃。
    """
    session_id: str
    seq: int
    rows: dict[str, CheckpointRow]
    format_version: int
    created_at: int
    cwd: str | None
    is_seeded: bool = False
    inherited_event_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "seq": self.seq,
            "format_version": self.format_version,
            "created_at": self.created_at,
            "cwd": self.cwd,
            "is_seeded": self.is_seeded,
            "inherited_event_count": self.inherited_event_count,
            "rows": {k: v.to_dict() for k, v in self.rows.items()},
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ProjectionCheckpoint | None:
        """从字典解析。旧格式（无 format_version）返回 None。"""
        if "format_version" not in data:
            return None
        rows = {}
        for k, v in data.get("rows", {}).items():
            try:
                rows[k] = CheckpointRow.from_dict(v)
            except (KeyError, TypeError):
                return None
        return cls(
            session_id=data["session_id"],
            seq=data.get("seq", 0),
            rows=rows,
            format_version=data["format_version"],
            created_at=data.get("created_at", 0),
            cwd=data.get("cwd"),
            is_seeded=data.get("is_seeded", False),
            inherited_event_count=data.get("inherited_event_count", 0),
        )

    @classmethod
    def from_session(cls, session: Any, rows: dict[str, CheckpointRow]) -> ProjectionCheckpoint:
        """从 Session 实例创建 checkpoint。"""
        return cls(
            session_id=session.id,
            seq=max(session.seq - 1, 0),
            rows=rows,
            format_version=FORMAT_VERSION,
            created_at=session.created_at,
            cwd=session.cwd,
            is_seeded=session.is_seeded,
            inherited_event_count=session.inherited_event_count,
        )


def identity_matches(checkpoint: ProjectionCheckpoint, header: SessionHeader) -> bool:
    """检查 checkpoint 身份是否匹配当前 session 生命周期。"""
    return (
        checkpoint.format_version == header.version
        and checkpoint.created_at == header.created_at
        and checkpoint.cwd == header.cwd
        and checkpoint.is_seeded == header.is_seeded
        and checkpoint.inherited_event_count == header.inherited_event_count
    )


@dataclass
class _ProjectionUnit:
    """注册的投影单元定义。"""
    key: str
    state_version: int
    init: Callable[[], Any]
    apply: Callable[[Any, dict[str, Any]], Any]


class ProjectionRegistry:
    """投影单元注册表。

    管理所有注册的投影单元，每个单元是一个纯同步 fold，带有 state_version。
    """

    def __init__(self):
        self._units: dict[str, _ProjectionUnit] = {}
        self._init_values: dict[str, Any] = {}

    def register(
        self,
        key: str,
        init: Callable[[], Any],
        apply: Callable[[Any, dict[str, Any]], Any],
        state_version: int = 1,
    ) -> None:
        if not isinstance(state_version, int) or state_version < 0:
            raise ValueError(f"state_version must be a non-negative integer, got {state_version}")
        self._units[key] = _ProjectionUnit(
            key=key,
            state_version=state_version,
            init=init,
            apply=apply,
        )
        self._init_values[key] = init()

    @property
    def units(self) -> dict[str, _ProjectionUnit]:
        return self._units

    def init_state(self) -> dict[str, Any]:
        return {k: v.init() for k, v in self._units.items()}

    def apply_event(self, state: dict[str, Any], event: dict[str, Any]) -> dict[str, Any]:
        new_state = dict(state)
        for key, unit in self._units.items():
            try:
                new_state[key] = unit.apply(state.get(key), event)
            except Exception as e:
                # 单个投影单元故障不拖垮整个 fold，但必须可见（禁止静默吞）
                print(f"[projcache] 投影单元 {key} 应用事件 {event.get('type')} 失败: {e!r}")
        return new_state

    def fold_events(self, init_val: dict[str, Any], events: list[dict[str, Any]]) -> dict[str, Any]:
        state = dict(init_val)
        for event in events:
            state = self.apply_event(state, event)
        return state

    def checkpoint(self, state: dict[str, Any], seq: int) -> dict[str, CheckpointRow]:
        rows = {}
        for key, unit in self._units.items():
            rows[key] = CheckpointRow(
                ver=unit.state_version,
                seq=seq,
                val=state.get(key),
            )
        return rows

    def restore(
        self,
        checkpoint: ProjectionCheckpoint,
        all_events: list[dict[str, Any]],
        header: SessionHeader,
    ) -> dict[str, Any]:
        """按 key 独立恢复：版本匹配的行从 checkpoint 继续 forward-apply，
        版本不匹配的行从 init 全量折叠。"""
        state: dict[str, Any] = {}
        for key, unit in self._units.items():
            row = checkpoint.rows.get(key)
            if row is not None and row.ver == unit.state_version:
                tail = [e for e in all_events if e.get("seq", 0) > row.seq]
                val = row.val
                for event in tail:
                    val = unit.apply(val, event)
                state[key] = val
            else:
                val = unit.init()
                for event in all_events:
                    val = unit.apply(val, event)
                state[key] = val
        return state


_projection_registry = ProjectionRegistry()


def register_projection(
    key: str,
    init: Callable[[], Any],
    apply: Callable[[Any, dict[str, Any]], Any],
    state_version: int = 1,
) -> None:
    _projection_registry.register(key, init, apply, state_version)


def get_projection_registry() -> ProjectionRegistry:
    return _projection_registry


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
    return event_time if event_time > state else state


def _init_cwd() -> str | None:
    return None


def _apply_cwd(state: str | None, event: dict[str, Any]) -> str | None:
    if event.get("type") == "session/created" and event.get("cwd"):
        return event.get("cwd")
    return state


def _init_running() -> bool:
    return False


def _apply_running(state: bool, event: dict[str, Any]) -> bool:
    t = event.get("type")
    if t == "turn/start":
        return True
    # D4 修复：abort 路径写 turn/cancel（sessions.py abort 端点），不认它则
    # 被中止会话在投影/前端永久"运行中"
    if t in ("turn/end", "turn/cancel"):
        return False
    return state


def _init_context_used() -> int:
    return 0


def _apply_context_used(state: int, event: dict[str, Any]) -> int:
    if event.get("type") != "stats":
        return state
    estimated = event.get("estimated_context_tokens")
    if estimated is not None:
        return int(estimated)
    return int(event.get("last_total_token_count", state))


def _init_context_total() -> int:
    return DEFAULT_CONTEXT_WINDOW


def _apply_context_total(state: int, event: dict[str, Any]) -> int:
    if event.get("type") != "stats":
        return state
    return int(event.get("effective_window", state))


def _init_plan_slug() -> str | None:
    return None


def _apply_plan_slug(state: str | None, event: dict[str, Any]) -> str | None:
    if event.get("type") == "session/plan_linked" and event.get("plan_slug"):
        return event.get("plan_slug")
    return state


def _init_meta_str() -> str | None:
    return None


def _apply_origin(state: str | None, event: dict[str, Any]) -> str | None:
    if event.get("type") == "session/meta" and event.get("origin"):
        return event.get("origin")
    return state


def _apply_parent_session(state: str | None, event: dict[str, Any]) -> str | None:
    if event.get("type") == "session/meta" and event.get("parent_session"):
        return event.get("parent_session")
    return state


def _apply_agent_type(state: str | None, event: dict[str, Any]) -> str | None:
    if event.get("type") == "session/meta" and event.get("agent_type"):
        return event.get("agent_type")
    return state


register_projection("title", _init_title, _apply_title, state_version=1)
register_projection("updated_at", _init_updated_at, _apply_updated_at, state_version=1)
register_projection("cwd", _init_cwd, _apply_cwd, state_version=1)
register_projection("running", _init_running, _apply_running, state_version=1)
register_projection("context_used", _init_context_used, _apply_context_used, state_version=2)
register_projection("context_total", _init_context_total, _apply_context_total, state_version=2)
register_projection("plan_slug", _init_plan_slug, _apply_plan_slug, state_version=1)
register_projection("origin", _init_meta_str, _apply_origin, state_version=1)
register_projection("parent_session", _init_meta_str, _apply_parent_session, state_version=1)
register_projection("agent_type", _init_meta_str, _apply_agent_type, state_version=1)


class ProjectionCache:
    """投影缓存管理器。"""

    def __init__(self, cache_dir: Path | None = None):
        self._cache_dir_override = cache_dir
        self._pending_writes: dict[str, int] = {}
        self._last_write_time: dict[str, float] = {}

        self.write_count_threshold = 10
        self.write_interval_threshold = 5.0

    @property
    def cache_dir(self) -> Path:
        d = Path(self._cache_dir_override) if self._cache_dir_override is not None else session_dir()
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _cache_path(self, session_id: str) -> Path:
        return self.cache_dir / f"{session_id}.projcache.json"

    def load_checkpoint(self, session_id: str) -> ProjectionCheckpoint | None:
        path = self._cache_path(session_id)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text())
            return ProjectionCheckpoint.from_dict(data)
        except Exception:
            return None

    def save_checkpoint(self, checkpoint: ProjectionCheckpoint) -> None:
        path = self._cache_path(checkpoint.session_id)
        try:
            path.write_text(json.dumps(checkpoint.to_dict(), indent=2, default=str))
            self._last_write_time[checkpoint.session_id] = time.time()
            self._pending_writes[checkpoint.session_id] = 0
        except Exception as e:
            print(f"[projcache] checkpoint 写入失败 {path.name}: {e!r}")

    def should_write(self, session_id: str) -> bool:
        count = self._pending_writes.get(session_id, 0)
        if count >= self.write_count_threshold:
            return True
        last_time = self._last_write_time.get(session_id, 0)
        if time.time() - last_time >= self.write_interval_threshold:
            return True
        return False

    def record_event(self, session_id: str) -> None:
        self._pending_writes[session_id] = self._pending_writes.get(session_id, 0) + 1

    def force_write(self, session_id: str) -> None:
        self._pending_writes[session_id] = self.write_count_threshold

    def delete_checkpoint(self, session_id: str) -> None:
        path = self._cache_path(session_id)
        if path.exists():
            path.unlink()
        self._pending_writes.pop(session_id, None)
        self._last_write_time.pop(session_id, None)


_projection_cache: ProjectionCache | None = None


def get_projection_cache() -> ProjectionCache:
    global _projection_cache
    if _projection_cache is None:
        _projection_cache = ProjectionCache()
    return _projection_cache


def build_projections_from_events(events: list[dict[str, Any]]) -> dict[str, Any]:
    registry = get_projection_registry()
    state = registry.init_state()
    return registry.fold_events(state, events)


def restore_projections(
    session_id: str,
    all_events: list[dict[str, Any]],
    header: SessionHeader,
) -> dict[str, Any]:
    """恢复投影状态。

    身份不匹配时丢弃旧 checkpoint，全量重建。
    版本不匹配的 key 独立重建。
    """
    cache = get_projection_cache()
    registry = get_projection_registry()

    checkpoint = cache.load_checkpoint(session_id)

    if checkpoint is not None and not identity_matches(checkpoint, header):
        checkpoint = None

    if checkpoint is not None:
        state = registry.restore(checkpoint, all_events, header)

        last_seq = all_events[-1].get("seq", 0) if all_events else 0
        if last_seq > checkpoint.seq:
            rows = registry.checkpoint(state, last_seq)
            new_checkpoint = ProjectionCheckpoint(
                session_id=session_id,
                seq=last_seq,
                rows=rows,
                format_version=FORMAT_VERSION,
                created_at=header.created_at,
                cwd=header.cwd,
                is_seeded=header.is_seeded,
                inherited_event_count=header.inherited_event_count,
            )
            cache.save_checkpoint(new_checkpoint)

        return state

    state = build_projections_from_events(all_events)

    if all_events:
        last_seq = all_events[-1].get("seq", 0)
        rows = registry.checkpoint(state, last_seq)
        checkpoint = ProjectionCheckpoint(
            session_id=session_id,
            seq=last_seq,
            rows=rows,
            format_version=FORMAT_VERSION,
            created_at=header.created_at,
            cwd=header.cwd,
            is_seeded=header.is_seeded,
            inherited_event_count=header.inherited_event_count,
        )
        cache.save_checkpoint(checkpoint)

    return state
