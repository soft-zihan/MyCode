"""PayloadStore — 大 payload 存储。

设计：
- 大 payload 存储在独立文件，事件日志只存储引用
- 减少事件日志大小，提高扫描性能
- 支持懒加载
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def _payload_dir() -> Path:
    """Payload 存储根目录。"""
    override = os.environ.get("MYCODE_PAYLOAD_DIR", "").strip()
    if override:
        return Path(override)
    return Path.home() / ".mycode" / "payloads"


class PayloadStore:
    """Payload 存储，大 payload 存储在独立文件。"""
    
    def __init__(self, session_id: str):
        self.session_id = session_id
        self.base_dir = _payload_dir() / session_id
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self._seq = 0
    
    def store(self, data: Any) -> str:
        """存储 payload，返回引用 ID。"""
        ref_id = f"p_{self._seq:06d}"
        self._seq += 1
        path = self.base_dir / f"{ref_id}.json"
        path.write_text(json.dumps(data, ensure_ascii=False, default=str))
        return ref_id
    
    def load(self, ref_id: str) -> Any:
        """加载 payload。"""
        path = self.base_dir / f"{ref_id}.json"
        if not path.exists():
            return None
        return json.loads(path.read_text())
    
    def delete(self, ref_id: str) -> bool:
        """删除 payload。"""
        path = self.base_dir / f"{ref_id}.json"
        if path.exists():
            path.unlink()
            return True
        return False
    
    def list_payloads(self) -> list[str]:
        """列出所有 payload 引用 ID。"""
        return [p.stem for p in self.base_dir.glob("p_*.json")]
    
    def clear(self) -> int:
        """清空所有 payload，返回删除数量。"""
        count = 0
        for p in self.base_dir.glob("p_*.json"):
            p.unlink()
            count += 1
        return count
    
    @classmethod
    def for_session(cls, session_id: str) -> PayloadStore:
        """为指定 session 创建 PayloadStore。"""
        return cls(session_id)


# 阈值：超过此大小的 payload 将存储在独立文件
PAYLOAD_THRESHOLD = 1000  # 字符数


def should_store_payload(data: Any) -> bool:
    """判断 payload 是否应该存储在独立文件。"""
    if isinstance(data, str):
        return len(data) > PAYLOAD_THRESHOLD
    if isinstance(data, dict):
        total = sum(len(str(v)) for v in data.values())
        return total > PAYLOAD_THRESHOLD
    if isinstance(data, list):
        total = sum(len(str(v)) for v in data)
        return total > PAYLOAD_THRESHOLD
    return False


def append_with_payload(session: Any, event_type: str, data: dict) -> dict:
    """追加事件，大 payload 存储在独立文件。
    
    返回事件字典（包含引用或内联数据）。
    """
    # 分离小字段和大字段
    small_fields = {}
    large_fields = {}
    
    for key, value in data.items():
        if should_store_payload(value):
            large_fields[key] = value
        else:
            small_fields[key] = value
    
    # 存储大 payload
    if large_fields:
        store = PayloadStore.for_session(session.id)
        ref_id = store.store(large_fields)
        small_fields["_payload_ref"] = ref_id
    
    # 写入事件日志
    return session.append(event_type, small_fields)


def resolve_payload(event: dict) -> dict:
    """解析事件中的 payload 引用。
    
    如果事件包含 _payload_ref，则加载并合并到事件中。
    """
    ref_id = event.get("_payload_ref")
    if not ref_id:
        return event
    
    session_id = event.get("session_id")
    if not session_id:
        return event
    
    store = PayloadStore.for_session(session_id)
    payload = store.load(ref_id)
    if payload is None:
        return event
    
    # 合并 payload 到事件
    result = {k: v for k, v in event.items() if k != "_payload_ref"}
    result.update(payload)
    return result
