"""全 Harness 进化数据结构与状态管理。

进化对象从单一 SKILL.md 扩展到四类 harness 组件：
- prompt: 补充性提示词片段（行为策略、风格约束）
- memory: 持久化事实与偏好（项目级记忆条目）
- skill: 可复用任务方法（SKILL.md）
- subagent: 子 Agent 配置（.bear/agents/*.md）

设计参考：Prime Agent / HCL (arXiv:2605.09998) 的 guarded harness evolution。
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field, asdict
from enum import Enum
from pathlib import Path

from agents._utils import utc_now as _utc_now
from typing import Any, Optional


class RefinementKind(str, Enum):
    """可进化的 harness 组件类型。"""
    PROMPT = "prompt"
    MEMORY = "memory"
    SKILL = "skill"
    SUBAGENT = "subagent"


class RefinementAction(str, Enum):
    """进化操作类型。"""
    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"


class HarnessScope(str, Enum):
    """进化作用域。"""
    LOCAL = "local"      # session 级（当前会话特有）
    GLOBAL = "global"    # 跨 session（稳定经验）


@dataclass
class RefinementEdit:
    """单次进化编辑操作。"""
    action: RefinementAction
    kind: RefinementKind
    target_id: Optional[str] = None      # update/delete 时必填
    title: Optional[str] = None
    content: Optional[str] = None
    path: Optional[str] = None           # 文件路径（用于落盘）
    reason: Optional[str] = None         # 编辑原因
    reference: Optional[dict] = None     # 元数据引用
    arguments: Optional[dict] = None     # 参数描述（skill 用）
    metadata: Optional[dict] = None      # 额外元数据

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["action"] = self.action.value
        d["kind"] = self.kind.value
        return {k: v for k, v in d.items() if v is not None}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> RefinementEdit:
        return cls(
            action=RefinementAction(d.get("action", "create")),
            kind=RefinementKind(d.get("kind", "skill")),
            target_id=d.get("target_id"),
            title=d.get("title"),
            content=d.get("content"),
            path=d.get("path"),
            reason=d.get("reason"),
            reference=d.get("reference"),
            arguments=d.get("arguments"),
            metadata=d.get("metadata"),
        )


@dataclass
class RefinementProposal:
    """进化提案（由 LLM 生成）。"""
    summary: str                          # 一句话总结
    rationale: str                        # 为什么需要这个改动
    edits: list[RefinementEdit]           # 具体编辑列表
    expected_outcome: str                 # 预期效果

    def to_dict(self) -> dict[str, Any]:
        return {
            "summary": self.summary,
            "rationale": self.rationale,
            "edits": [e.to_dict() for e in self.edits],
            "expected_outcome": self.expected_outcome,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> RefinementProposal:
        return cls(
            summary=d.get("summary", ""),
            rationale=d.get("rationale", ""),
            edits=[RefinementEdit.from_dict(e) for e in d.get("edits", [])],
            expected_outcome=d.get("expected_outcome", ""),
        )


@dataclass
class HarnessEntry:
    """单个 harness 组件的状态记录。"""
    id: str
    kind: RefinementKind
    title: str
    content: str
    path: str                             # 文件路径
    scope: HarnessScope = HarnessScope.LOCAL
    source: str = ""                      # 来源描述
    created_at: str = ""
    updated_at: str = ""
    version: int = 1
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["kind"] = self.kind.value
        d["scope"] = self.scope.value
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> HarnessEntry:
        return cls(
            id=d.get("id", ""),
            kind=RefinementKind(d.get("kind", "skill")),
            title=d.get("title", ""),
            content=d.get("content", ""),
            path=d.get("path", ""),
            scope=HarnessScope(d.get("scope", "local")),
            source=d.get("source", ""),
            created_at=d.get("created_at", ""),
            updated_at=d.get("updated_at", ""),
            version=d.get("version", 1),
            metadata=d.get("metadata", {}),
        )


@dataclass
class RefinementEvent:
    """一次进化事件记录（用于审计）。"""
    id: str
    trigger: str                          # "manual" | "auto" | "turn_interval" | "compact"
    summary: str
    rationale: str
    expected_outcome: str
    applied_edits: list[dict]             # 实际应用的编辑（含 before/after）
    scope: HarnessScope = HarnessScope.LOCAL
    created_at: str = ""
    rollback_of: Optional[str] = None     # 如果是回滚，指向原事件 ID

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["scope"] = self.scope.value
        return {k: v for k, v in d.items() if v is not None}


@dataclass
class HarnessState:
    """Harness 整体状态（持久化到 JSON）。"""
    schema_version: int = 1
    entries: dict[str, dict[str, HarnessEntry]] = field(default_factory=dict)
    refinements: list[RefinementEvent] = field(default_factory=list)

    def __post_init__(self):
        # 确保四个 kind 都有空 dict
        for kind in RefinementKind:
            if kind.value not in self.entries:
                self.entries[kind.value] = {}

    def get_entry(self, kind: RefinementKind, entry_id: str) -> Optional[HarnessEntry]:
        return self.entries.get(kind.value, {}).get(entry_id)

    def set_entry(self, entry: HarnessEntry) -> None:
        if entry.kind.value not in self.entries:
            self.entries[entry.kind.value] = {}
        self.entries[entry.kind.value][entry.id] = entry

    def delete_entry(self, kind: RefinementKind, entry_id: str) -> Optional[HarnessEntry]:
        return self.entries.get(kind.value, {}).pop(entry_id, None)

    def list_entries(self, kind: Optional[RefinementKind] = None) -> list[HarnessEntry]:
        if kind:
            return list(self.entries.get(kind.value, {}).values())
        result = []
        for kind_entries in self.entries.values():
            result.extend(kind_entries.values())
        return result

    def add_refinement(self, event: RefinementEvent) -> None:
        self.refinements.append(event)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "entries": {
                kind: {eid: entry.to_dict() for eid, entry in entries.items()}
                for kind, entries in self.entries.items()
            },
            "refinements": [r.to_dict() for r in self.refinements],
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> HarnessState:
        state = cls(schema_version=d.get("schema_version", 1))
        for kind_str, entries_dict in d.get("entries", {}).items():
            for eid, entry_data in entries_dict.items():
                state.entries.setdefault(kind_str, {})[eid] = HarnessEntry.from_dict(entry_data)
        for r_data in d.get("refinements", []):
            event = RefinementEvent(
                id=r_data.get("id", ""),
                trigger=r_data.get("trigger", ""),
                summary=r_data.get("summary", ""),
                rationale=r_data.get("rationale", ""),
                expected_outcome=r_data.get("expected_outcome", ""),
                applied_edits=r_data.get("applied_edits", []),
                scope=HarnessScope(r_data.get("scope", "local")),
                created_at=r_data.get("created_at", ""),
                rollback_of=r_data.get("rollback_of"),
            )
            state.refinements.append(event)
        return state


# ── 持久化 ──────────────────────────────────────────────────────────────────


def get_harness_state_dir() -> Path:
    """获取 harness state 存储目录。"""
    return Path.cwd() / ".bear" / "harness-state"


def get_harness_state_path() -> Path:
    return get_harness_state_dir() / "harness.json"


def get_refinement_history_path() -> Path:
    return get_harness_state_dir() / "refinements.jsonl"


def load_harness_state() -> HarnessState:
    """从磁盘加载 harness state。"""
    path = get_harness_state_path()
    if not path.is_file():
        return HarnessState()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return HarnessState.from_dict(data)
    except Exception:
        return HarnessState()


def save_harness_state(state: HarnessState) -> None:
    """保存 harness state 到磁盘。"""
    path = get_harness_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(state.to_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def append_refinement_log(event: RefinementEvent) -> None:
    """追加进化事件到 JSONL 日志（审计用）。"""
    path = get_refinement_history_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event.to_dict(), ensure_ascii=False, sort_keys=True) + "\n")


# ── 辅助函数 ──────────────────────────────────────────────────────────────────


def generate_entry_id(kind: RefinementKind, title: str) -> str:
    """生成 entry ID（kind-slug 格式）。"""
    slug = re.sub(r"[^A-Za-z0-9_.\-]+", "-", title.lower()).strip("-")[:40]
    return f"{kind.value}-{slug}"


def resolve_component_path(kind: RefinementKind, name: str) -> Path:
    """根据组件类型和名称解析文件路径。"""
    cwd = Path.cwd()
    if kind == RefinementKind.PROMPT:
        return cwd / ".bear" / "prompts" / f"{name}.md"
    elif kind == RefinementKind.MEMORY:
        # Memory 路径需要项目 hash，这里简化处理
        return cwd / ".bear" / "memories" / f"{name}.md"
    elif kind == RefinementKind.SKILL:
        return cwd / ".bear" / "skills" / name / "SKILL.md"
    elif kind == RefinementKind.SUBAGENT:
        return cwd / ".bear" / "agents" / f"{name}.md"
    else:
        raise ValueError(f"Unknown kind: {kind}")


def list_component_files(kind: RefinementKind) -> list[Path]:
    """列出某类型组件的所有文件。"""
    cwd = Path.cwd()
    if kind == RefinementKind.PROMPT:
        base = cwd / ".bear" / "prompts"
        return list(base.glob("*.md")) if base.is_dir() else []
    elif kind == RefinementKind.MEMORY:
        base = cwd / ".bear" / "memories"
        return list(base.glob("*.md")) if base.is_dir() else []
    elif kind == RefinementKind.SKILL:
        base = cwd / ".bear" / "skills"
        return [p / "SKILL.md" for p in base.iterdir() if p.is_dir()] if base.is_dir() else []
    elif kind == RefinementKind.SUBAGENT:
        base = cwd / ".bear" / "agents"
        return list(base.glob("*.md")) if base.is_dir() else []
    else:
        return []
