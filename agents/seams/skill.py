"""Skill Seam 接口定义。

Skill 存储抽象，支持多种实现：
- FileSkillStore：文件型 Skill（当前 BearCode 的方式）
- CustomSkillStore：自定义 Skill
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


# ============================================================
# 数据类型
# ============================================================


@dataclass
class SkillDefinition:
    """Skill 定义。"""
    
    name: str
    description: str
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)
    
    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "content": self.content,
            "metadata": self.metadata,
        }
    
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SkillDefinition:
        return cls(
            name=data["name"],
            description=data["description"],
            content=data["content"],
            metadata=data.get("metadata", {}),
        )


# ============================================================
# SkillStore 接口
# ============================================================


class SkillStore(Protocol):
    """Skill 存储协议。
    
    所有 Skill 存储实现必须遵守此接口。
    """
    
    def get(self, name: str) -> SkillDefinition | None:
        """获取 Skill 定义。
        
        Args:
            name: Skill 名称
        
        Returns:
            Skill 定义，不存在则返回 None
        """
        ...
    
    def list(self) -> list[SkillDefinition]:
        """列出所有 Skill。
        
        Returns:
            Skill 定义列表
        """
        ...
    
    def register(self, skill: SkillDefinition) -> None:
        """注册 Skill。
        
        Args:
            skill: Skill 定义
        """
        ...
    
    def unregister(self, name: str) -> bool:
        """注销 Skill。
        
        Args:
            name: Skill 名称
        
        Returns:
            是否注销成功
        """
        ...
    
    def search(self, query: str) -> list[SkillDefinition]:
        """搜索 Skill。
        
        Args:
            query: 搜索关键词
        
        Returns:
            匹配的 Skill 定义列表
        """
        ...


# ============================================================
# FileSkillStore 实现
# ============================================================


class FileSkillStore:
    """文件型 Skill 存储（当前 BearCode 的方式）。"""
    
    def __init__(self, skills_dir: str | None = None) -> None:
        from pathlib import Path
        self._skills_dir = Path(skills_dir) if skills_dir else self._default_skills_dir()
        self._skills_dir.mkdir(parents=True, exist_ok=True)
    
    def _default_skills_dir(self) -> Path:
        from pathlib import Path
        import os
        override = os.environ.get("BEAR_SKILLS_DIR", "").strip()
        if override:
            return Path(override)
        return Path.home() / ".bear-code" / "skills"
    
    def _get_file_path(self, name: str) -> Path:
        return self._skills_dir / f"{name}.md"
    
    def get(self, name: str) -> SkillDefinition | None:
        path = self._get_file_path(name)
        if not path.exists():
            return None
        
        try:
            content = path.read_text()
            # 解析 frontmatter
            lines = content.split("\n")
            description = ""
            body_lines = []
            in_frontmatter = False
            frontmatter_done = False
            
            for i, line in enumerate(lines):
                if i == 0 and line.strip() == "---":
                    in_frontmatter = True
                    continue
                if in_frontmatter and line.strip() == "---":
                    in_frontmatter = False
                    frontmatter_done = True
                    continue
                if in_frontmatter:
                    if line.startswith("description:"):
                        description = line[len("description:"):].strip().strip('"').strip("'")
                else:
                    body_lines.append(line)
            
            body = "\n".join(body_lines).strip()
            
            return SkillDefinition(
                name=name,
                description=description,
                content=body,
                metadata={"path": str(path)},
            )
        except Exception:
            return None
    
    def list(self) -> list[SkillDefinition]:
        skills = []
        for path in self._skills_dir.glob("*.md"):
            name = path.stem
            skill = self.get(name)
            if skill:
                skills.append(skill)
        return skills
    
    def register(self, skill: SkillDefinition) -> None:
        path = self._get_file_path(skill.name)
        content = f"""---
description: "{skill.description}"
---

{skill.content}
"""
        path.write_text(content)
    
    def unregister(self, name: str) -> bool:
        path = self._get_file_path(name)
        if path.exists():
            path.unlink()
            return True
        return False
    
    def search(self, query: str) -> list[SkillDefinition]:
        skills = self.list()
        query_lower = query.lower()
        return [
            s for s in skills
            if query_lower in s.name.lower()
            or query_lower in s.description.lower()
            or query_lower in s.content.lower()
        ]
