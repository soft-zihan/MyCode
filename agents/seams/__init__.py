"""Capability Seam 接口定义。

借鉴 Pi 的 Capability Seam 架构模式，将功能模块抽象为可替换接口。
每个 Seam 可以有多种后端实现，构造时注入。
"""

from .session import (
    SessionEntry,
    SessionStorage,
    InMemorySessionStorage,
    JsonSessionStorage,
    JsonlTreeSessionStorage,
)
from .session_adapter import LegacySessionStorage
from .compaction import (
    CompactionResult,
    CompactionStrategy,
    FourLevelCompaction,
    SingleLevelCompaction,
)
from .provider import (
    StreamEvent,
    Provider,
    OpenAIProvider,
    AnthropicProvider,
)
from .memory import (
    MemoryEntry,
    MemoryStore,
    FileMemoryStore,
    SQLiteMemoryStore,
)
from .skill import (
    SkillDefinition,
    SkillStore,
    FileSkillStore,
)

__all__ = [
    "SessionEntry",
    "SessionStorage",
    "InMemorySessionStorage",
    "JsonSessionStorage",
    "JsonlTreeSessionStorage",
    "LegacySessionStorage",
    "CompactionResult",
    "CompactionStrategy",
    "FourLevelCompaction",
    "SingleLevelCompaction",
    "StreamEvent",
    "Provider",
    "OpenAIProvider",
    "AnthropicProvider",
    "MemoryEntry",
    "MemoryStore",
    "FileMemoryStore",
    "SQLiteMemoryStore",
    "SkillDefinition",
    "SkillStore",
    "FileSkillStore",
]
