"""Memory Seam 接口定义。

Memory 存储抽象。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from agents.core.session import atomic_write_json


# ============================================================
# 数据类型
# ============================================================


@dataclass
class MemoryEntry:
    """Memory 条目。"""
    
    id: str
    category: str  # "user" | "project" | "session" | "global"
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)
    timestamp: str = ""
    
    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "category": self.category,
            "content": self.content,
            "metadata": self.metadata,
            "timestamp": self.timestamp,
        }
    
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MemoryEntry:
        return cls(
            id=data["id"],
            category=data["category"],
            content=data["content"],
            metadata=data.get("metadata", {}),
            timestamp=data.get("timestamp", ""),
        )


# ============================================================
# MemoryStore 接口
# ============================================================


class MemoryStore(Protocol):
    """Memory 存储协议。
    
    所有 Memory 存储实现必须遵守此接口。
    """
    
    def get(self, category: str, key: str) -> str | None: ...
    def set(self, category: str, key: str, value: str) -> None: ...
    def delete(self, category: str, key: str) -> bool: ...
    def list(self, category: str | None = None) -> list[MemoryEntry]: ...
    def clear(self, category: str | None = None) -> None: ...
    def search(self, query: str, category: str | None = None) -> list[MemoryEntry]: ...


# ============================================================
# FileMemoryStore 实现
# ============================================================


class FileMemoryStore:
    """文件型 Memory 存储（当前 BearCode 的方式）。"""
    
    def __init__(self, memory_dir: str | None = None) -> None:
        from pathlib import Path
        self._memory_dir = Path(memory_dir) if memory_dir else self._default_memory_dir()
        self._memory_dir.mkdir(parents=True, exist_ok=True)
    
    def _default_memory_dir(self) -> Path:
        from pathlib import Path
        import os
        override = os.environ.get("BEAR_MEMORY_DIR", "").strip()
        if override:
            return Path(override)
        return Path.home() / ".bear-code" / "memory"
    
    def _get_file_path(self, category: str) -> Path:
        return self._memory_dir / f"{category}.json"
    
    def _load(self, category: str) -> dict[str, Any]:
        path = self._get_file_path(category)
        if not path.exists():
            return {}
        try:
            import json
            return json.loads(path.read_text())
        except Exception:
            return {}
    
    def _save(self, category: str, data: dict[str, Any]) -> None:
        import json
        path = self._get_file_path(category)
        atomic_write_json(path, data)
    
    def get(self, category: str, key: str) -> str | None:
        data = self._load(category)
        return data.get(key)
    
    def set(self, category: str, key: str, value: str) -> None:
        data = self._load(category)
        data[key] = value
        self._save(category, data)
    
    def delete(self, category: str, key: str) -> bool:
        data = self._load(category)
        if key in data:
            del data[key]
            self._save(category, data)
            return True
        return False
    
    def list(self, category: str | None = None) -> list[MemoryEntry]:
        import time
        categories = [category] if category else ["user", "project", "session", "global"]
        entries = []
        for cat in categories:
            data = self._load(cat)
            for key, value in data.items():
                entries.append(MemoryEntry(
                    id=f"{cat}:{key}",
                    category=cat,
                    content=str(value),
                    metadata={"key": key},
                    timestamp=time.strftime("%Y-%m-%dT%H:%M:%S"),
                ))
        return entries
    
    def clear(self, category: str | None = None) -> None:
        if category:
            self._save(category, {})
        else:
            for cat in ["user", "project", "session", "global"]:
                self._save(cat, {})
    
    def search(self, query: str, category: str | None = None) -> list[MemoryEntry]:
        entries = self.list(category)
        query_lower = query.lower()
        return [
            e for e in entries
            if query_lower in e.content.lower() or query_lower in e.metadata.get("key", "").lower()
        ]
