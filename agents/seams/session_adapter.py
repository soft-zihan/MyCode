"""Session Storage 适配器。

将现有的 session.py 函数包装成符合 SessionStorage 接口的实现。
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

from agents.session import (
    load_session,
    save_session,
    session_dir,
)


class LegacySessionStorage:
    """Legacy Session Storage 适配器。
    
    将现有的 session.py 函数包装成符合 SessionStorage 接口的实现。
    这是一个过渡方案，允许渐进式迁移。
    """
    
    def __init__(self, session_id: str) -> None:
        self._session_id = session_id
        self._data: dict[str, Any] | None = None
        self._load()
    
    def _load(self) -> None:
        """加载会话数据。"""
        self._data = load_session(self._session_id)
        if self._data is None:
            self._data = {
                "metadata": {
                    "id": self._session_id,
                    "startTime": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                },
                "entries": [],
                "leaf_id": None,
                "label": None,
            }
    
    def _save(self) -> None:
        """保存会话数据。"""
        if self._data is not None:
            save_session(self._session_id, self._data)
    
    def create_entry_id(self) -> str:
        """生成唯一条目 ID。"""
        return uuid.uuid4().hex[:16]
    
    def create_timestamp(self) -> str:
        """生成时间戳。"""
        return time.strftime("%Y-%m-%dT%H:%M:%S.", time.gmtime()) + f"{int(time.time() * 1000) % 1000:03d}"
    
    def get_metadata(self) -> dict[str, Any]:
        """获取会话元数据。"""
        if self._data is None:
            return {}
        return dict(self._data.get("metadata", {}))
    
    def set_metadata(self, metadata: dict[str, Any]) -> None:
        """设置会话元数据。"""
        if self._data is None:
            self._data = {"metadata": {}, "entries": [], "leaf_id": None, "label": None}
        self._data["metadata"] = dict(metadata)
        self._save()
    
    def get_entries(self) -> list[dict[str, Any]]:
        """获取所有条目（按时间顺序）。"""
        if self._data is None:
            return []
        return list(self._data.get("entries", []))
    
    def get_entry(self, entry_id: str) -> dict[str, Any] | None:
        """获取指定条目。"""
        if self._data is None:
            return None
        for e in self._data.get("entries", []):
            if e.get("id") == entry_id:
                return e
        return None
    
    def append_entry(self, entry: dict[str, Any]) -> None:
        """追加条目。"""
        if self._data is None:
            self._data = {"metadata": {}, "entries": [], "leaf_id": None, "label": None}
        if "entries" not in self._data:
            self._data["entries"] = []
        self._data["entries"].append(entry)
        self._data["leaf_id"] = entry.get("id")
        self._save()
    
    def get_leaf_id(self) -> str | None:
        """获取当前叶节点 ID。"""
        if self._data is None:
            return None
        return self._data.get("leaf_id")
    
    def set_leaf_id(self, entry_id: str | None) -> None:
        """设置当前叶节点 ID。"""
        if self._data is None:
            self._data = {"metadata": {}, "entries": [], "leaf_id": None, "label": None}
        self._data["leaf_id"] = entry_id
        self._save()
    
    def get_label(self) -> str | None:
        """获取会话标签。"""
        if self._data is None:
            return None
        return self._data.get("label")
    
    def set_label(self, label: str | None) -> None:
        """设置会话标签。"""
        if self._data is None:
            self._data = {"metadata": {}, "entries": [], "leaf_id": None, "label": None}
        self._data["label"] = label
        self._save()
    
    def fork(self, new_session_id: str) -> "LegacySessionStorage":
        """从当前状态 fork 出新会话。"""
        new_storage = LegacySessionStorage(new_session_id)
        if self._data is not None:
            new_storage._data = json.loads(json.dumps(self._data))
            new_storage._save()
        return new_storage
