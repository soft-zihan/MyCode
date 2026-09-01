"""Session Seam 接口定义。

会话存储后端抽象。
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
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
    """会话存储后端协议。"""
    
    def create_entry_id(self) -> str: ...
    def create_timestamp(self) -> str: ...
    def get_metadata(self) -> dict[str, Any]: ...
    def set_metadata(self, metadata: dict[str, Any]) -> None: ...
    def get_entries(self) -> list[SessionEntry]: ...
    def get_entry(self, entry_id: str) -> SessionEntry | None: ...
    def append_entry(self, entry: SessionEntry) -> None: ...
    def get_leaf_id(self) -> str | None: ...
    def set_leaf_id(self, entry_id: str | None) -> None: ...
    def get_label(self) -> str | None: ...
    def set_label(self, label: str | None) -> None: ...
    def fork(self, new_session_id: str) -> "SessionStorage": ...
