"""默认 Memory Store 实现。

封装现有的 Memory 逻辑，使其符合 MemoryStore 接口。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from agents.seams.memory import MemoryEntry


class DefaultMemoryStore:
    """默认 Memory Store（封装现有的文件型 Memory）。
    
    这是一个适配器，将现有的 Memory 逻辑包装成 MemoryStore 接口。
    """
    
    def __init__(self, memory_dir: str | None = None) -> None:
        self._memory_dir = Path(memory_dir) if memory_dir else self._default_memory_dir()
        self._memory_dir.mkdir(parents=True, exist_ok=True)
    
    def _default_memory_dir(self) -> Path:
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
            return json.loads(path.read_text())
        except Exception:
            return {}
    
    def _save(self, category: str, data: dict[str, Any]) -> None:
        path = self._get_file_path(category)
        path.write_text(json.dumps(data, indent=2, default=str))
    
    def get(self, category: str, key: str) -> str | None:
        """获取 Memory 值。"""
        data = self._load(category)
        return data.get(key)
    
    def set(self, category: str, key: str, value: str) -> None:
        """设置 Memory 值。"""
        data = self._load(category)
        data[key] = value
        self._save(category, data)
    
    def delete(self, category: str, key: str) -> bool:
        """删除 Memory 值。"""
        data = self._load(category)
        if key in data:
            del data[key]
            self._save(category, data)
            return True
        return False
    
    def list(self, category: str | None = None) -> list[MemoryEntry]:
        """列出 Memory 条目。"""
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
        """清空 Memory。"""
        if category:
            self._save(category, {})
        else:
            for cat in ["user", "project", "session", "global"]:
                self._save(cat, {})
    
    def search(self, query: str, category: str | None = None) -> list[MemoryEntry]:
        """搜索 Memory。"""
        entries = self.list(category)
        query_lower = query.lower()
        return [
            e for e in entries
            if query_lower in e.content.lower() or query_lower in e.metadata.get("key", "").lower()
        ]
