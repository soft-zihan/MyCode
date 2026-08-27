"""Phase 6: Capability Seam 接口测试。

测试 Seam 接口的各种实现。
"""

import json
import tempfile
from pathlib import Path

import pytest

from agents.seams import (
    SessionEntry,
    InMemorySessionStorage,
    JsonSessionStorage,
    JsonlTreeSessionStorage,
    LegacySessionStorage,
    CompactionResult,
    MemoryEntry,
    FileMemoryStore,
    SQLiteMemoryStore,
    SkillDefinition,
    FileSkillStore,
)


# ============================================================
# Session Seam 测试
# ============================================================


class TestInMemorySessionStorage:
    """InMemorySessionStorage 测试。"""
    
    def test_create_entry_id(self):
        storage = InMemorySessionStorage()
        entry_id = storage.create_entry_id()
        assert isinstance(entry_id, str)
        assert len(entry_id) == 16
    
    def test_create_timestamp(self):
        storage = InMemorySessionStorage()
        timestamp = storage.create_timestamp()
        assert isinstance(timestamp, str)
        assert "T" in timestamp
    
    def test_append_and_get_entry(self):
        storage = InMemorySessionStorage()
        entry = SessionEntry(
            id=storage.create_entry_id(),
            parent_id=None,
            timestamp=storage.create_timestamp(),
            type="message",
            data={"role": "user", "content": "hello"},
        )
        storage.append_entry(entry)
        
        retrieved = storage.get_entry(entry.id)
        assert retrieved is not None
        assert retrieved.id == entry.id
        assert retrieved.data == entry.data
    
    def test_get_entries(self):
        storage = InMemorySessionStorage()
        for i in range(3):
            entry = SessionEntry(
                id=storage.create_entry_id(),
                parent_id=None,
                timestamp=storage.create_timestamp(),
                type="message",
                data={"index": i},
            )
            storage.append_entry(entry)
        
        entries = storage.get_entries()
        assert len(entries) == 3
        assert entries[0].data["index"] == 0
        assert entries[2].data["index"] == 2
    
    def test_leaf_id(self):
        storage = InMemorySessionStorage()
        assert storage.get_leaf_id() is None
        
        entry = SessionEntry(
            id=storage.create_entry_id(),
            parent_id=None,
            timestamp=storage.create_timestamp(),
            type="message",
        )
        storage.append_entry(entry)
        assert storage.get_leaf_id() == entry.id
    
    def test_metadata(self):
        storage = InMemorySessionStorage()
        storage.set_metadata({"key": "value"})
        assert storage.get_metadata() == {"key": "value"}
    
    def test_label(self):
        storage = InMemorySessionStorage()
        assert storage.get_label() is None
        storage.set_label("test-label")
        assert storage.get_label() == "test-label"
    
    def test_fork(self):
        storage = InMemorySessionStorage()
        entry = SessionEntry(
            id=storage.create_entry_id(),
            parent_id=None,
            timestamp=storage.create_timestamp(),
            type="message",
            data={"content": "hello"},
        )
        storage.append_entry(entry)
        
        forked = storage.fork("new-session")
        assert forked.get_entry(entry.id) is not None
        assert forked.get_leaf_id() == entry.id


class TestJsonSessionStorage:
    """JsonSessionStorage 测试。"""
    
    def test_persistence(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            session_dir = Path(tmpdir)
            storage = JsonSessionStorage("test-session", session_dir)
            
            entry = SessionEntry(
                id=storage.create_entry_id(),
                parent_id=None,
                timestamp=storage.create_timestamp(),
                type="message",
                data={"content": "hello"},
            )
            storage.append_entry(entry)
            
            # 重新加载
            storage2 = JsonSessionStorage("test-session", session_dir)
            retrieved = storage2.get_entry(entry.id)
            assert retrieved is not None
            assert retrieved.data == entry.data


class TestJsonlTreeSessionStorage:
    """JsonlTreeSessionStorage 测试。"""
    
    def test_persistence(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            file_path = Path(tmpdir) / "test.jsonl"
            storage = JsonlTreeSessionStorage(file_path)
            
            entry = SessionEntry(
                id=storage.create_entry_id(),
                parent_id=None,
                timestamp=storage.create_timestamp(),
                type="message",
                data={"content": "hello"},
            )
            storage.append_entry(entry)
            
            # 重新加载
            storage2 = JsonlTreeSessionStorage(file_path)
            retrieved = storage2.get_entry(entry.id)
            assert retrieved is not None
            assert retrieved.data == entry.data


class TestLegacySessionStorage:
    """LegacySessionStorage 测试。"""
    
    def test_adapter(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            import os
            os.environ["BEAR_SESSION_DIR"] = tmpdir
            
            try:
                storage = LegacySessionStorage("test-session")
                entry_id = storage.create_entry_id()
                entry = {
                    "id": entry_id,
                    "parent_id": None,
                    "timestamp": storage.create_timestamp(),
                    "type": "message",
                    "data": {"content": "hello"},
                }
                storage.append_entry(entry)
                
                retrieved = storage.get_entry(entry_id)
                assert retrieved is not None
                assert retrieved["data"] == entry["data"]
            finally:
                del os.environ["BEAR_SESSION_DIR"]


# ============================================================
# Memory Seam 测试
# ============================================================


class TestFileMemoryStore:
    """FileMemoryStore 测试。"""
    
    def test_get_set(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = FileMemoryStore(tmpdir)
            store.set("user", "name", "Alice")
            assert store.get("user", "name") == "Alice"
    
    def test_delete(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = FileMemoryStore(tmpdir)
            store.set("user", "name", "Alice")
            assert store.delete("user", "name") is True
            assert store.get("user", "name") is None
    
    def test_list(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = FileMemoryStore(tmpdir)
            store.set("user", "name", "Alice")
            store.set("user", "age", "30")
            entries = store.list("user")
            assert len(entries) == 2
    
    def test_search(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = FileMemoryStore(tmpdir)
            store.set("user", "name", "Alice")
            store.set("user", "email", "alice@example.com")
            results = store.search("alice")
            assert len(results) == 2


class TestSQLiteMemoryStore:
    """SQLiteMemoryStore 测试。"""
    
    def test_get_set(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "memory.db"
            store = SQLiteMemoryStore(str(db_path))
            store.set("user", "name", "Alice")
            assert store.get("user", "name") == "Alice"
    
    def test_delete(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "memory.db"
            store = SQLiteMemoryStore(str(db_path))
            store.set("user", "name", "Alice")
            assert store.delete("user", "name") is True
            assert store.get("user", "name") is None
    
    def test_list(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "memory.db"
            store = SQLiteMemoryStore(str(db_path))
            store.set("user", "name", "Alice")
            store.set("user", "age", "30")
            entries = store.list("user")
            assert len(entries) == 2


# ============================================================
# Skill Seam 测试
# ============================================================


class TestFileSkillStore:
    """FileSkillStore 测试。"""
    
    def test_register_and_get(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = FileSkillStore(tmpdir)
            skill = SkillDefinition(
                name="test-skill",
                description="A test skill",
                content="This is the skill content.",
            )
            store.register(skill)
            
            retrieved = store.get("test-skill")
            assert retrieved is not None
            assert retrieved.name == "test-skill"
            assert retrieved.description == "A test skill"
    
    def test_list(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = FileSkillStore(tmpdir)
            store.register(SkillDefinition("skill1", "desc1", "content1"))
            store.register(SkillDefinition("skill2", "desc2", "content2"))
            skills = store.list()
            assert len(skills) == 2
    
    def test_unregister(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = FileSkillStore(tmpdir)
            store.register(SkillDefinition("test-skill", "desc", "content"))
            assert store.unregister("test-skill") is True
            assert store.get("test-skill") is None
    
    def test_search(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = FileSkillStore(tmpdir)
            store.register(SkillDefinition("python-skill", "Python programming", "content"))
            store.register(SkillDefinition("javascript-skill", "JavaScript programming", "content"))
            results = store.search("python")
            assert len(results) == 1
            assert results[0].name == "python-skill"


# ============================================================
# AgentOptions 测试
# ============================================================


class TestAgentOptions:
    """AgentOptions 测试。"""
    
    def test_default_values(self):
        from agents.options import AgentOptions
        options = AgentOptions()
        assert options.permission_mode == "default"
        assert options.model == "deepseek-chat"
        assert options.thinking is False
    
    def test_to_dict(self):
        from agents.options import AgentOptions
        options = AgentOptions(model="gpt-4", thinking=True)
        data = options.to_dict()
        assert data["model"] == "gpt-4"
        assert data["thinking"] is True
    
    def test_from_dict(self):
        from agents.options import AgentOptions
        data = {"model": "gpt-4", "thinking": True}
        options = AgentOptions.from_dict(data)
        assert options.model == "gpt-4"
        assert options.thinking is True
    
    def test_with_overrides(self):
        from agents.options import AgentOptions
        options = AgentOptions(model="gpt-4")
        new_options = options.with_overrides(thinking=True)
        assert new_options.model == "gpt-4"
        assert new_options.thinking is True
