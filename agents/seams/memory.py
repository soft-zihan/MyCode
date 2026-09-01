"""Memory Seam 接口定义。

Memory 存储抽象，支持多种实现：
- FileMemoryStore：文件型 Memory（当前 BearCode 的方式）
- SQLiteMemoryStore：SQLite Memory
- CustomMemoryStore：自定义 Memory
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from agents.session import atomic_write_json


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
    
    def get(self, category: str, key: str) -> str | None:
        """获取 Memory 值。
        
        Args:
            category: 类别（user/project/session/global）
            key: 键名
        
        Returns:
            值，不存在则返回 None
        """
        ...
    
    def set(self, category: str, key: str, value: str) -> None:
        """设置 Memory 值。
        
        Args:
            category: 类别
            key: 键名
            value: 值
        """
        ...
    
    def delete(self, category: str, key: str) -> bool:
        """删除 Memory 值。
        
        Args:
            category: 类别
            key: 键名
        
        Returns:
            是否删除成功
        """
        ...
    
    def list(self, category: str | None = None) -> list[MemoryEntry]:
        """列出 Memory 条目。
        
        Args:
            category: 类别（None 表示所有类别）
        
        Returns:
            Memory 条目列表
        """
        ...
    
    def clear(self, category: str | None = None) -> None:
        """清空 Memory。
        
        Args:
            category: 类别（None 表示清空所有）
        """
        ...
    
    def search(self, query: str, category: str | None = None) -> list[MemoryEntry]:
        """搜索 Memory。
        
        Args:
            query: 搜索关键词
            category: 类别（None 表示所有类别）
        
        Returns:
            匹配的 Memory 条目列表
        """
        ...


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


# ============================================================
# SQLiteMemoryStore 实现
# ============================================================


class SQLiteMemoryStore:
    """SQLite Memory 存储。"""
    
    def __init__(self, db_path: str | None = None) -> None:
        from pathlib import Path
        self._db_path = Path(db_path) if db_path else self._default_db_path()
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()
    
    def _default_db_path(self) -> Path:
        from pathlib import Path
        import os
        override = os.environ.get("BEAR_MEMORY_DB", "").strip()
        if override:
            return Path(override)
        return Path.home() / ".bear-code" / "memory.db"
    
    def _init_db(self) -> None:
        import sqlite3
        conn = sqlite3.connect(str(self._db_path))
        conn.execute("""
            CREATE TABLE IF NOT EXISTS memory (
                id TEXT PRIMARY KEY,
                category TEXT NOT NULL,
                key TEXT NOT NULL,
                value TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                UNIQUE(category, key)
            )
        """)
        conn.commit()
        conn.close()
    
    def get(self, category: str, key: str) -> str | None:
        import sqlite3
        conn = sqlite3.connect(str(self._db_path))
        cursor = conn.execute(
            "SELECT value FROM memory WHERE category = ? AND key = ?",
            (category, key)
        )
        row = cursor.fetchone()
        conn.close()
        return row[0] if row else None
    
    def set(self, category: str, key: str, value: str) -> None:
        import sqlite3
        import time
        conn = sqlite3.connect(str(self._db_path))
        timestamp = time.strftime("%Y-%m-%dT%H:%M:%S")
        conn.execute(
            """
            INSERT INTO memory (id, category, key, value, timestamp)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(category, key) DO UPDATE SET
                value = excluded.value,
                timestamp = excluded.timestamp
            """,
            (f"{category}:{key}", category, key, value, timestamp)
        )
        conn.commit()
        conn.close()
    
    def delete(self, category: str, key: str) -> bool:
        import sqlite3
        conn = sqlite3.connect(str(self._db_path))
        cursor = conn.execute(
            "DELETE FROM memory WHERE category = ? AND key = ?",
            (category, key)
        )
        conn.commit()
        conn.close()
        return cursor.rowcount > 0
    
    def list(self, category: str | None = None) -> list[MemoryEntry]:
        import sqlite3
        conn = sqlite3.connect(str(self._db_path))
        if category:
            cursor = conn.execute(
                "SELECT id, category, key, value, timestamp FROM memory WHERE category = ?",
                (category,)
            )
        else:
            cursor = conn.execute(
                "SELECT id, category, key, value, timestamp FROM memory"
            )
        
        entries = []
        for row in cursor.fetchall():
            entries.append(MemoryEntry(
                id=row[0],
                category=row[1],
                content=row[3],
                metadata={"key": row[2]},
                timestamp=row[4],
            ))
        conn.close()
        return entries
    
    def clear(self, category: str | None = None) -> None:
        import sqlite3
        conn = sqlite3.connect(str(self._db_path))
        if category:
            conn.execute("DELETE FROM memory WHERE category = ?", (category,))
        else:
            conn.execute("DELETE FROM memory")
        conn.commit()
        conn.close()
    
    def search(self, query: str, category: str | None = None) -> list[MemoryEntry]:
        import sqlite3
        conn = sqlite3.connect(str(self._db_path))
        query_pattern = f"%{query}%"
        
        if category:
            cursor = conn.execute(
                """
                SELECT id, category, key, value, timestamp FROM memory
                WHERE category = ? AND (value LIKE ? OR key LIKE ?)
                """,
                (category, query_pattern, query_pattern)
            )
        else:
            cursor = conn.execute(
                """
                SELECT id, category, key, value, timestamp FROM memory
                WHERE value LIKE ? OR key LIKE ?
                """,
                (query_pattern, query_pattern)
            )
        
        entries = []
        for row in cursor.fetchall():
            entries.append(MemoryEntry(
                id=row[0],
                category=row[1],
                content=row[3],
                metadata={"key": row[2]},
                timestamp=row[4],
            ))
        conn.close()
        return entries
