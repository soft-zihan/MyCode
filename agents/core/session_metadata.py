"""Session metadata management.

DeepSeek 对齐：
- 独立的 SessionHeader 结构
- 元数据持久化到存储后端
- 支持列表查询和快速访问
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .session import session_dir


@dataclass
class SessionHeader:
    """Session 元数据头。
    
    DeepSeek 对齐：存储 session 级别的基本信息，
    用于列表查询和快速访问，不需要加载完整事件日志。
    
    Attributes:
        id: Session ID
        version: Session format version
        created_at: 创建时间戳
        cwd: 工作目录
        parent_session: 父 session ID（fork 链）
        origin: 来源标记（'sub_agent' 等）
        agent_type: 智能体类型
        title: Session 标题
        delegation_depth: 委派深度（子智能体层级）
    """
    id: str
    version: int = 1
    created_at: float = field(default_factory=time.time)
    cwd: str | None = None
    parent_session: str | None = None
    origin: str | None = None
    agent_type: str | None = None
    title: str | None = None
    delegation_depth: int = 0
    
    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "version": self.version,
            "created_at": self.created_at,
            "cwd": self.cwd,
            "parent_session": self.parent_session,
            "origin": self.origin,
            "agent_type": self.agent_type,
            "title": self.title,
            "delegation_depth": self.delegation_depth,
        }
    
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SessionHeader:
        return cls(
            id=data["id"],
            version=data.get("version", 1),
            created_at=data.get("created_at", time.time()),
            cwd=data.get("cwd"),
            parent_session=data.get("parent_session"),
            origin=data.get("origin"),
            agent_type=data.get("agent_type"),
            title=data.get("title"),
            delegation_depth=data.get("delegation_depth", 0),
        )


class SessionMetadataStore:
    """Session 元数据存储。
    
    管理 session 元数据的持久化和查询。
    """
    
    def __init__(self, metadata_dir: Path | None = None):
        if metadata_dir is None:
            metadata_dir = session_dir()
        self.metadata_dir = metadata_dir
        self.metadata_dir.mkdir(parents=True, exist_ok=True)
    
    def _header_path(self, session_id: str) -> Path:
        return self.metadata_dir / f"{session_id}.header.json"
    
    def save_header(self, header: SessionHeader) -> None:
        """保存 session header。"""
        path = self._header_path(header.id)
        try:
            path.write_text(json.dumps(header.to_dict(), indent=2, default=str))
        except Exception:
            pass  # 写入失败不影响主流程
    
    def load_header(self, session_id: str) -> SessionHeader | None:
        """加载 session header。"""
        path = self._header_path(session_id)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text())
            return SessionHeader.from_dict(data)
        except Exception:
            return None
    
    def list_headers(self) -> list[SessionHeader]:
        """列出所有 session headers。"""
        headers = []
        for path in self.metadata_dir.glob("*.header.json"):
            try:
                data = json.loads(path.read_text())
                headers.append(SessionHeader.from_dict(data))
            except Exception:
                continue
        return headers
    
    def delete_header(self, session_id: str) -> None:
        """删除 session header。"""
        path = self._header_path(session_id)
        if path.exists():
            path.unlink()
    
    def update_header(self, session_id: str, updates: dict[str, Any]) -> None:
        """更新 session header 的部分字段。"""
        header = self.load_header(session_id)
        if header is None:
            return
        
        for key, value in updates.items():
            if hasattr(header, key):
                setattr(header, key, value)
        
        self.save_header(header)


# 全局实例
_metadata_store: SessionMetadataStore | None = None


def get_metadata_store() -> SessionMetadataStore:
    """获取全局元数据存储实例。"""
    global _metadata_store
    if _metadata_store is None:
        _metadata_store = SessionMetadataStore()
    return _metadata_store


def create_session_header(
    session_id: str,
    cwd: str | None = None,
    parent_session: str | None = None,
    origin: str | None = None,
    agent_type: str | None = None,
) -> SessionHeader:
    """创建并保存 session header。"""
    header = SessionHeader(
        id=session_id,
        cwd=cwd,
        parent_session=parent_session,
        origin=origin,
        agent_type=agent_type,
    )
    store = get_metadata_store()
    store.save_header(header)
    return header


def get_session_header(session_id: str) -> SessionHeader | None:
    """获取 session header。"""
    store = get_metadata_store()
    return store.load_header(session_id)


def list_session_headers() -> list[SessionHeader]:
    """列出所有 session headers。"""
    store = get_metadata_store()
    return store.list_headers()
