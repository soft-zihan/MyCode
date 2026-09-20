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
    
    # 3. Side Query 提示词
    # 这些是独立的提示词文件，不包括 hidden agent 对应的文件
    side_query_dir = _PROMPTS_DIR / "side_query"
    if side_query_dir.exists():
        side_query_descriptions = {
            "compile_session": "编译会话笔记",
            "consolidate_wiki": "审核 Wiki 相关性",
            "distill_chunk": "蒸馏对话块",
            "explore": "技术调研",
            "extract_goal": "提取目标标准",
            "extract_knowledge": "提取持久知识",
            "generate_skill": "生成 Skill",
            "select_memories": "选择相关记忆",
            "select_wiki": "选择相关 Wiki 条目",
            "verify_goal": "验证目标达成",
        }
        
        # 这些文件已经在 hidden agent 中处理，不需要重复加载
        hidden_agent_files = {
            "select_memories.txt",
            "select_wiki.txt",
            "compile_session.txt",
            "generate_skill.txt",
            "extract_goal.txt",
        }
        
        for f in sorted(side_query_dir.glob("*.txt")):
            # 跳过已经在 hidden agent 中处理的文件
            if f.name in hidden_agent_files:
                continue
                
            prompts.append(PromptInfo(
                name=f"side_query:{f.stem}",
                category="side_query",
                description=side_query_descriptions.get(f.stem, f"Side Query: {f.stem}"),
                source=str(f),
                editable=True,
                content=f.read_text(encoding="utf-8"),
            ))
    
    # 4. Hidden Agent 提示词（归类为 side_query）
    # 这些 agent 的提示词在 agent_mode.py 中定义，或者从文件加载
    user_agents_dir = Path.home() / ".mycode" / "agents"
    
    # 定义 hidden agent 到提示词文件的映射
    hidden_agent_prompt_files = {
        "side_query_memory": "select_memories.txt",
        "side_query_wiki": "select_wiki.txt",
        "side_query_compile": "compile_session.txt",
        "side_query_skill": "generate_skill.txt",
        "side_query_goal": "extract_goal.txt",
    }
    
    for name, config in BUILTIN_HIDDEN_AGENTS.items():
        override_path = user_agents_dir / f"{name}.md"
        has_override = override_path.exists()
        
        # 如果有 override 文件，读取完整内容（包括 frontmatter）
        if has_override:
            content = override_path.read_text(encoding="utf-8")
            source = str(override_path)
        else:
            # 检查是否有对应的提示词文件
            prompt_file = hidden_agent_prompt_files.get(name)
            if prompt_file:
                prompt_path = _PROMPTS_DIR / "side_query" / prompt_file
                if prompt_path.exists():
                    content = prompt_path.read_text(encoding="utf-8")
                    source = str(prompt_path)
                else:
                    # 文件不存在，使用 agent_mode.py 中的提示词
                    from agents.memory.frontmatter import format_frontmatter
                    meta = {"name": name, "description": config.description}
                    content = format_frontmatter(meta, config.system_prompt)
                    source = "builtin"
            else:
                # 没有对应的提示词文件，使用 agent_mode.py 中的提示词
                from agents.memory.frontmatter import format_frontmatter
                meta = {"name": name, "description": config.description}
                content = format_frontmatter(meta, config.system_prompt)
                source = "builtin"
        
        # 使用更清晰的名称
        display_name = name.replace("side_query_", "")
        prompts.append(PromptInfo(
            name=f"side_query:{display_name}",
            category="side_query",
            description=config.description,
            source=source,
            editable=True,
            content=content,
        ))
    
    # 5. Plan Mode 策略文件
    strategies_dir = Path(__file__).parent.parent / "plan" / "strategies"
    if strategies_dir.exists():
        strategy_descriptions = {
            "grill-spec": "需求澄清策略",
            "tasks": "任务拆分策略",
            "execute": "执行策略",
            "review": "审查策略",
            "converge": "收敛策略",
        }
        for category_dir in sorted(strategies_dir.iterdir()):
            if category_dir.is_dir():
                category = category_dir.name
                for f in sorted(category_dir.glob("*.md")):
                    prompts.append(PromptInfo(
                        name=f"plan_strategy:{category}/{f.stem}",
                        category="plan",
                        description=f"{strategy_descriptions.get(category, category)}: {f.stem}",
                        source=str(f),
                        editable=True,
                        content=f.read_text(encoding="utf-8"),
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
