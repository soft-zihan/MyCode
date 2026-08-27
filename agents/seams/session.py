"""Session Seam 接口定义。

会话存储后端抽象，支持多种实现：
- InMemorySessionStorage：内存存储（测试用）
- JsonSessionStorage：JSON 文件存储（当前 BearCode 的方式）
- JsonlTreeSessionStorage：JSONL 树形存储（Pi 的方式）
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol


# ============================================================
# 数据类型
# ============================================================


@dataclass
class SessionEntry:
    """会话树的一个条目。"""
    
    id: str
    parent_id: str | None
    timestamp: str
    type: str  # "message" | "compaction" | "branch_summary" | "session_info"
    data: Any = None
    label: str | None = None
    
    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "parent_id": self.parent_id,
            "timestamp": self.timestamp,
            "type": self.type,
            "data": self.data,
            "label": self.label,
        }
    
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SessionEntry:
        return cls(
            id=data["id"],
            parent_id=data.get("parent_id"),
            timestamp=data["timestamp"],
            type=data["type"],
            data=data.get("data"),
            label=data.get("label"),
        )


def _create_entry_id() -> str:
    """生成唯一条目 ID。"""
    return uuid.uuid4().hex[:16]


def _create_timestamp() -> str:
    """生成 ISO 时间戳。"""
    return time.strftime("%Y-%m-%dT%H:%M:%S.", time.gmtime()) + f"{int(time.time() * 1000) % 1000:03d}"


# ============================================================
# SessionStorage 接口
# ============================================================


class SessionStorage(Protocol):
    """会话存储后端协议。
    
    所有会话存储实现必须遵守此接口。
    """
    
    def create_entry_id(self) -> str:
        """生成唯一条目 ID。"""
        ...
    
    def create_timestamp(self) -> str:
        """生成时间戳。"""
        ...
    
    def get_metadata(self) -> dict[str, Any]:
        """获取会话元数据。"""
        ...
    
    def set_metadata(self, metadata: dict[str, Any]) -> None:
        """设置会话元数据。"""
        ...
    
    def get_entries(self) -> list[SessionEntry]:
        """获取所有条目（按时间顺序）。"""
        ...
    
    def get_entry(self, entry_id: str) -> SessionEntry | None:
        """获取指定条目。"""
        ...
    
    def append_entry(self, entry: SessionEntry) -> None:
        """追加条目。"""
        ...
    
    def get_leaf_id(self) -> str | None:
        """获取当前叶节点 ID。"""
        ...
    
    def set_leaf_id(self, entry_id: str | None) -> None:
        """设置当前叶节点 ID。"""
        ...
    
    def get_label(self) -> str | None:
        """获取会话标签。"""
        ...
    
    def set_label(self, label: str | None) -> None:
        """设置会话标签。"""
        ...
    
    def fork(self, new_session_id: str) -> "SessionStorage":
        """从当前状态 fork 出新会话。"""
        ...


# ============================================================
# InMemorySessionStorage 实现
# ============================================================


class InMemorySessionStorage:
    """纯内存会话存储（测试用）。"""
    
    def __init__(self, metadata: dict[str, Any] | None = None) -> None:
        self._metadata = metadata or {}
        self._entries: dict[str, SessionEntry] = {}
        self._order: list[str] = []
        self._leaf_id: str | None = None
        self._label: str | None = None
    
    def create_entry_id(self) -> str:
        return _create_entry_id()
    
    def create_timestamp(self) -> str:
        return _create_timestamp()
    
    def get_metadata(self) -> dict[str, Any]:
        return dict(self._metadata)
    
    def set_metadata(self, metadata: dict[str, Any]) -> None:
        self._metadata = dict(metadata)
    
    def get_entries(self) -> list[SessionEntry]:
        return [self._entries[eid] for eid in self._order if eid in self._entries]
    
    def get_entry(self, entry_id: str) -> SessionEntry | None:
        return self._entries.get(entry_id)
    
    def append_entry(self, entry: SessionEntry) -> None:
        self._entries[entry.id] = entry
        self._order.append(entry.id)
        self._leaf_id = entry.id
    
    def get_leaf_id(self) -> str | None:
        return self._leaf_id
    
    def set_leaf_id(self, entry_id: str | None) -> None:
        self._leaf_id = entry_id
    
    def get_label(self) -> str | None:
        return self._label
    
    def set_label(self, label: str | None) -> None:
        self._label = label
    
    def fork(self, new_session_id: str) -> "InMemorySessionStorage":
        """Fork 出新会话（深拷贝）。"""
        new_storage = InMemorySessionStorage(metadata=dict(self._metadata))
        new_storage._entries = {
            eid: SessionEntry.from_dict(e.to_dict())
            for eid, e in self._entries.items()
        }
        new_storage._order = list(self._order)
        new_storage._leaf_id = self._leaf_id
        new_storage._label = self._label
        return new_storage


# ============================================================
# JsonSessionStorage 实现
# ============================================================


class JsonSessionStorage:
    """JSON 文件会话存储（当前 BearCode 的方式）。"""
    
    def __init__(self, session_id: str, session_dir: Path | None = None) -> None:
        self._session_id = session_id
        self._session_dir = session_dir or self._default_session_dir()
        self._path = self._session_dir / f"{session_id}.json"
        self._data: dict[str, Any] = self._load()
    
    def _default_session_dir(self) -> Path:
        import os
        override = os.environ.get("BEAR_SESSION_DIR", "").strip()
        if override:
            return Path(override)
        return Path.home() / ".bear-code" / "sessions"
    
    def _load(self) -> dict[str, Any]:
        if not self._path.exists():
            return {
                "metadata": {},
                "entries": [],
                "leaf_id": None,
                "label": None,
            }
        try:
            return json.loads(self._path.read_text())
        except Exception:
            return {
                "metadata": {},
                "entries": [],
                "leaf_id": None,
                "label": None,
            }
    
    def _save(self) -> None:
        self._session_dir.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(self._data, indent=2, default=str))
    
    def create_entry_id(self) -> str:
        return _create_entry_id()
    
    def create_timestamp(self) -> str:
        return _create_timestamp()
    
    def get_metadata(self) -> dict[str, Any]:
        return dict(self._data.get("metadata", {}))
    
    def set_metadata(self, metadata: dict[str, Any]) -> None:
        self._data["metadata"] = dict(metadata)
        self._save()
    
    def get_entries(self) -> list[SessionEntry]:
        return [SessionEntry.from_dict(e) for e in self._data.get("entries", [])]
    
    def get_entry(self, entry_id: str) -> SessionEntry | None:
        for e in self._data.get("entries", []):
            if e["id"] == entry_id:
                return SessionEntry.from_dict(e)
        return None
    
    def append_entry(self, entry: SessionEntry) -> None:
        if "entries" not in self._data:
            self._data["entries"] = []
        self._data["entries"].append(entry.to_dict())
        self._data["leaf_id"] = entry.id
        self._save()
    
    def get_leaf_id(self) -> str | None:
        return self._data.get("leaf_id")
    
    def set_leaf_id(self, entry_id: str | None) -> None:
        self._data["leaf_id"] = entry_id
        self._save()
    
    def get_label(self) -> str | None:
        return self._data.get("label")
    
    def set_label(self, label: str | None) -> None:
        self._data["label"] = label
        self._save()
    
    def fork(self, new_session_id: str) -> "JsonSessionStorage":
        """Fork 出新会话（复制 JSON 文件）。"""
        new_storage = JsonSessionStorage(new_session_id, self._session_dir)
        new_storage._data = json.loads(json.dumps(self._data))
        new_storage._path = self._session_dir / f"{new_session_id}.json"
        new_storage._save()
        return new_storage


# ============================================================
# JsonlTreeSessionStorage 实现
# ============================================================


class JsonlTreeSessionStorage:
    """JSONL 树形会话存储（Pi 的方式）。
    
    文件格式：
    - 第 1 行：metadata JSON（含 leaf_id / label）
    - 第 2+ 行：entry JSON（按追加顺序）
    """
    
    def __init__(self, file_path: str | Path, metadata: dict[str, Any] | None = None) -> None:
        self._path = Path(file_path)
        self._metadata: dict[str, Any] = metadata or {}
        self._entries: dict[str, SessionEntry] = {}
        self._order: list[str] = []
        self._leaf_id: str | None = None
        self._label: str | None = None
        self._load()
    
    def _load(self) -> None:
        if not self._path.exists():
            return
        
        lines = self._path.read_text().splitlines()
        if not lines:
            return
        
        # 第 1 行：metadata
        try:
            meta = json.loads(lines[0])
            self._metadata = meta.get("metadata", {})
            self._leaf_id = meta.get("leaf_id")
            self._label = meta.get("label")
        except Exception:
            pass
        
        # 第 2+ 行：entries
        for line in lines[1:]:
            if not line.strip():
                continue
            try:
                entry = SessionEntry.from_dict(json.loads(line))
                self._entries[entry.id] = entry
                self._order.append(entry.id)
            except Exception:
                continue
    
    def _save_metadata(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        meta = {
            "metadata": self._metadata,
            "leaf_id": self._leaf_id,
            "label": self._label,
        }
        # 重写整个文件（metadata + entries）
        lines = [json.dumps(meta, default=str)]
        for eid in self._order:
            if eid in self._entries:
                lines.append(json.dumps(self._entries[eid].to_dict(), default=str))
        self._path.write_text("\n".join(lines) + "\n")
    
    def create_entry_id(self) -> str:
        return _create_entry_id()
    
    def create_timestamp(self) -> str:
        return _create_timestamp()
    
    def get_metadata(self) -> dict[str, Any]:
        return dict(self._metadata)
    
    def set_metadata(self, metadata: dict[str, Any]) -> None:
        self._metadata = dict(metadata)
        self._save_metadata()
    
    def get_entries(self) -> list[SessionEntry]:
        return [self._entries[eid] for eid in self._order if eid in self._entries]
    
    def get_entry(self, entry_id: str) -> SessionEntry | None:
        return self._entries.get(entry_id)
    
    def append_entry(self, entry: SessionEntry) -> None:
        self._entries[entry.id] = entry
        self._order.append(entry.id)
        self._leaf_id = entry.id
        # 追加写入（高效）
        with self._path.open("a") as f:
            f.write(json.dumps(entry.to_dict(), default=str) + "\n")
        # 更新 metadata
        self._save_metadata()
    
    def get_leaf_id(self) -> str | None:
        return self._leaf_id
    
    def set_leaf_id(self, entry_id: str | None) -> None:
        self._leaf_id = entry_id
        self._save_metadata()
    
    def get_label(self) -> str | None:
        return self._label
    
    def set_label(self, label: str | None) -> None:
        self._label = label
        self._save_metadata()
    
    def fork(self, new_session_id: str) -> "JsonlTreeSessionStorage":
        """Fork 出新会话（复制 JSONL 文件）。"""
        new_path = self._path.parent / f"{new_session_id}.jsonl"
        if self._path.exists():
            import shutil
            shutil.copy2(self._path, new_path)
        return JsonlTreeSessionStorage(new_path, metadata=dict(self._metadata))
