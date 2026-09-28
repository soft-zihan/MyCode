"""提示词注册表 — 集中管理所有系统提示词。

提供统一的接口来列出、获取和编辑所有提示词组件。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agents.core.agent_mode import BUILTIN_HIDDEN_AGENTS
from agents.core.frontmatter import format_frontmatter


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


def _collect_main_prompt() -> list[PromptInfo]:
    """1. 主系统提示词。"""
    prompts: list[PromptInfo] = []
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
    return prompts


def _collect_subagent_prompts() -> list[PromptInfo]:
    """2. 子 Agent 提示词。"""
    prompts: list[PromptInfo] = []
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
    return prompts


# Side Query 提示词描述（不含 hidden agent 对应文件）
_SIDE_QUERY_DESCRIPTIONS = {
    "compile_session": "编译会话笔记",
    "consolidate_v2": "Wiki 整理（git-diff 驱动）",
    "explore": "技术调研",
    "extract_goal": "提取目标标准",
    "extract_knowledge": "提取持久知识",
    "generate_skill": "生成 Skill",
    "select_wiki": "选择相关 Wiki 条目",
    "verify_goal": "验证目标达成",
}

# 这些文件已经在 hidden agent 中处理，不需要重复加载
_HIDDEN_AGENT_FILES = {
    "select_wiki.txt",
    "compile_session.txt",
    "generate_skill.txt",
    "extract_goal.txt",
}


def _collect_side_query_prompts() -> list[PromptInfo]:
    """3. Side Query 提示词（独立文件，排除 hidden agent 已覆盖的）。"""
    prompts: list[PromptInfo] = []
    side_query_dir = _PROMPTS_DIR / "side_query"
    if not side_query_dir.exists():
        return prompts
    for f in sorted(side_query_dir.glob("*.txt")):
        # 跳过已经在 hidden agent 中处理的文件
        if f.name in _HIDDEN_AGENT_FILES:
            continue
        prompts.append(PromptInfo(
            name=f"side_query:{f.stem}",
            category="side_query",
            description=_SIDE_QUERY_DESCRIPTIONS.get(f.stem, f"Side Query: {f.stem}"),
            source=str(f),
            editable=True,
            content=f.read_text(encoding="utf-8"),
        ))
    return prompts


# hidden agent 到提示词文件的映射
_HIDDEN_AGENT_PROMPT_FILES = {
    "side_query_title": "generate_title.txt",
    "side_query_wiki": "select_wiki.txt",
    "side_query_compile": "compile_session.txt",
    "side_query_skill": "generate_skill.txt",
    "side_query_goal": "extract_goal.txt",
}


def _collect_hidden_agent_prompts() -> list[PromptInfo]:
    """4. Hidden Agent 提示词（归类为 side_query）。

    优先级：用户 override (~/.mycode/agents/<name>.md) > 提示词文件 > agent_mode.py 内置。
    """
    prompts: list[PromptInfo] = []
    user_agents_dir = Path.home() / ".mycode" / "agents"

    for name, config in BUILTIN_HIDDEN_AGENTS.items():
        override_path = user_agents_dir / f"{name}.md"
        if override_path.exists():
            # 如果有 override 文件，读取完整内容（包括 frontmatter）
            content = override_path.read_text(encoding="utf-8")
            source = str(override_path)
        else:
            prompt_file = _HIDDEN_AGENT_PROMPT_FILES.get(name)
            prompt_path = _PROMPTS_DIR / "side_query" / prompt_file if prompt_file else None
            if prompt_path is not None and prompt_path.exists():
                content = prompt_path.read_text(encoding="utf-8")
                source = str(prompt_path)
            else:
                # 没有可用的提示词文件，使用 agent_mode.py 中的提示词
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
    return prompts


_PLAN_STRATEGY_DESCRIPTIONS = {
    "grill-spec": "需求澄清策略",
    "tasks": "任务拆分策略",
    "execute": "执行策略",
    "review": "审查策略",
    "converge": "收敛策略",
}


def _collect_plan_strategy_prompts() -> list[PromptInfo]:
    """5. Plan Mode 策略文件。"""
    prompts: list[PromptInfo] = []
    strategies_dir = Path(__file__).parent.parent / "plan" / "strategies"
    if not strategies_dir.exists():
        return prompts
    for category_dir in sorted(strategies_dir.iterdir()):
        if not category_dir.is_dir():
            continue
        category = category_dir.name
        for f in sorted(category_dir.glob("*.md")):
            prompts.append(PromptInfo(
                name=f"plan_strategy:{category}/{f.stem}",
                category="plan",
                description=f"{_PLAN_STRATEGY_DESCRIPTIONS.get(category, category)}: {f.stem}",
                source=str(f),
                editable=True,
                content=f.read_text(encoding="utf-8"),
            ))
    return prompts


def list_all_prompts() -> list[PromptInfo]:
    """列出所有提示词。"""
    prompts: list[PromptInfo] = []
    prompts.extend(_collect_main_prompt())
    prompts.extend(_collect_subagent_prompts())
    prompts.extend(_collect_side_query_prompts())
    prompts.extend(_collect_hidden_agent_prompts())
    prompts.extend(_collect_plan_strategy_prompts())
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
