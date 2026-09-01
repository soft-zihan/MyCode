"""Capability Seam 接口定义。

借鉴 Pi 的 Capability Seam 架构模式，将功能模块抽象为可替换接口。
每个 Seam 可以有多种后端实现，构造时注入。
"""

from .session import (
    SessionEntry,
    SessionStorage,
)
from .session_adapter import LegacySessionStorage
from .compaction import (
    CompactionResult,
    CompactionStrategy,
)
from .compaction_default import DefaultCompactionStrategy
from .provider import (
    StreamEvent,
    Provider,
)
from .provider_default import DefaultProvider
from .memory import (
    MemoryEntry,
    MemoryStore,
    FileMemoryStore,
)
from .memory_default import DefaultMemoryStore
from .skill import (
    SkillDefinition,
    SkillStore,
    FileSkillStore,
)
from .skill_default import DefaultSkillStore

__all__ = [
    "SessionEntry",
    "SessionStorage",
    "LegacySessionStorage",
    "CompactionResult",
    "CompactionStrategy",
    "DefaultCompactionStrategy",
    "StreamEvent",
    "Provider",
    "DefaultProvider",
    "MemoryEntry",
    "MemoryStore",
    "FileMemoryStore",
    "DefaultMemoryStore",
    "SkillDefinition",
    "SkillStore",
    "FileSkillStore",
    "DefaultSkillStore",
]
