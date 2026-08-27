"""默认 Skill Store 实现。

封装现有的 Skill 逻辑，使其符合 SkillStore 接口。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from agents.seams.skill import SkillDefinition


class DefaultSkillStore:
    """默认 Skill Store（封装现有的文件型 Skill）。
    
    这是一个适配器，将现有的 Skill 逻辑包装成 SkillStore 接口。
    """
    
    def __init__(self, skills_dir: str | None = None) -> None:
        self._skills_dir = Path(skills_dir) if skills_dir else self._default_skills_dir()
        self._skills_dir.mkdir(parents=True, exist_ok=True)
    
    def _default_skills_dir(self) -> Path:
        import os
        override = os.environ.get("BEAR_SKILLS_DIR", "").strip()
        if override:
            return Path(override)
        return Path.home() / ".bear-code" / "skills"
    
    def _get_file_path(self, name: str) -> Path:
        return self._skills_dir / f"{name}.md"
    
    def get(self, name: str) -> SkillDefinition | None:
        """获取 Skill 定义。"""
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
        """列出所有 Skill。"""
        skills = []
        for path in self._skills_dir.glob("*.md"):
            name = path.stem
            skill = self.get(name)
            if skill:
                skills.append(skill)
        return skills
    
    def register(self, skill: SkillDefinition) -> None:
        """注册 Skill。"""
        path = self._get_file_path(skill.name)
        content = f"""---
description: "{skill.description}"
---

{skill.content}
"""
        path.write_text(content)
    
    def unregister(self, name: str) -> bool:
        """注销 Skill。"""
        path = self._get_file_path(name)
        if path.exists():
            path.unlink()
            return True
        return False
    
    def search(self, query: str) -> list[SkillDefinition]:
        """搜索 Skill。"""
        skills = self.list()
        query_lower = query.lower()
        return [
            s for s in skills
            if query_lower in s.name.lower()
            or query_lower in s.description.lower()
            or query_lower in s.content.lower()
        ]
