"""提示词注册表 — 集中管理所有系统提示词。

提供统一的接口来列出、获取和编辑所有提示词组件。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agents.core.agent_mode import BUILTIN_HIDDEN_AGENTS


@dataclass
class PromptInfo:
    """提示词信息。"""
    name: str
    category: str  # main, subagent, hidden, side_query
    description: str
    source: str  # file path or "builtin"
    editable: bool = True
    content: str = ""


_PROMPTS_DIR = Path(__file__).parent.parent / "prompts"


def list_all_prompts() -> list[PromptInfo]:
    """列出所有提示词。"""
    prompts: list[PromptInfo] = []
    
    # 1. 主系统提示词
    system_txt = _PROMPTS_DIR / "system.txt"
    if system_txt.exists():
        prompts.append(PromptInfo(
            name="system",
            category="main",
            description="主系统提示词模板",
            source=str(system_txt),
            editable=True,
            content=system_txt.read_text(encoding="utf-8"),
        ))
    
    # 2. 子 Agent 提示词
    subagent_dir = _PROMPTS_DIR / "subagent"
    if subagent_dir.exists():
        for f in sorted(subagent_dir.glob("*.txt")):
            prompts.append(PromptInfo(
                name=f"subagent:{f.stem}",
                category="subagent",
                description=f"子 Agent: {f.stem}",
                source=str(f),
                editable=True,
                content=f.read_text(encoding="utf-8"),
            ))
    
    # 3. Hidden Agent 提示词
    user_agents_dir = Path.home() / ".mycode" / "agents"
    for name, config in BUILTIN_HIDDEN_AGENTS.items():
        override_path = user_agents_dir / f"{name}.md"
        has_override = override_path.exists()
        
        # 如果有 override 文件，读取完整内容（包括 frontmatter）
        if has_override:
            content = override_path.read_text(encoding="utf-8")
            source = str(override_path)
        else:
            # 如果没有 override，生成包含 frontmatter 的初始内容
            from agents.memory.frontmatter import format_frontmatter
            meta = {"name": name, "description": config.description}
            content = format_frontmatter(meta, config.system_prompt)
            source = str(override_path)
        
        prompts.append(PromptInfo(
            name=f"hidden:{name}",
            category="hidden",
            description=config.description,
            source=source,
            editable=True,
            content=content,
        ))
    
    return prompts


def get_prompt(name: str) -> PromptInfo | None:
    """获取指定提示词。"""
    for p in list_all_prompts():
        if p.name == name:
            return p
    return None


def save_prompt(name: str, content: str) -> bool:
    """保存提示词。"""
    prompt = get_prompt(name)
    if not prompt or not prompt.editable:
        return False
    
    if prompt.source == "builtin":
        # Hidden agent 提示词保存到用户目录
        user_agents_dir = Path.home() / ".mycode" / "agents"
        user_agents_dir.mkdir(parents=True, exist_ok=True)
        
        # 提取 agent name
        agent_name = name.replace("hidden:", "")
        override_path = user_agents_dir / f"{agent_name}.md"
        
        # 写入 frontmatter + content
        from agents.memory.frontmatter import format_frontmatter
        meta = {"name": agent_name, "description": BUILTIN_HIDDEN_AGENTS.get(agent_name, None) and BUILTIN_HIDDEN_AGENTS[agent_name].description or ""}
        override_path.write_text(format_frontmatter(meta, content), encoding="utf-8")
        return True
    
    # 文件提示词直接写入
    source_path = Path(prompt.source)
    source_path.write_text(content, encoding="utf-8")
    return True


def get_prompt_summary() -> list[dict[str, Any]]:
    """获取提示词摘要（不含内容）。"""
    return [
        {
            "name": p.name,
            "category": p.category,
            "description": p.description,
            "source": p.source,
            "editable": p.editable,
            "content_length": len(p.content),
        }
        for p in list_all_prompts()
    ]
