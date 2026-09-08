"""Skill Hook 机制 —— 基于关键词匹配的直接召回

当用户消息匹配 Skill 定义的 hook-keywords 时，直接召回该 Skill，
跳过 BM25 检索，实现确定性的 Skill 触发。

Hook 模式：
- inject: 只注入 Skill prompt 到上下文（默认）
- inline: 同 inject，注入到对话上下文
- fork: 强制启动子 Agent 执行 Skill
"""

from __future__ import annotations

import re
from typing import Any


def match_skill_hooks(
    user_message: str,
    skills: list[Any],
) -> list[dict[str, Any]]:
    """检查用户消息是否匹配任何 Skill 的 hook-keywords。
    
    Args:
        user_message: 用户输入的消息
        skills: 所有已加载的 Skill 列表
    
    Returns:
        匹配的 Skill 列表，每个元素包含：
        - name: Skill 名称
        - prompt: Skill prompt（含参数替换）
        - context: 执行模式（inject/inline/fork）
        - allowed_tools: 工具白名单
        - model: 指定模型
        - matched_keywords: 匹配到的关键词列表
    """
    if not user_message or not skills:
        return []
    
    message_lower = user_message.lower()
    matched: list[dict[str, Any]] = []
    
    for skill in skills:
        hook_keywords = getattr(skill, "hook_keywords", None)
        if not hook_keywords:
            continue
        
        # 检查是否有关键词匹配
        matched_kw = []
        for keyword in hook_keywords:
            keyword_lower = keyword.lower().strip()
            if not keyword_lower:
                continue
            # 词边界匹配：避免部分匹配
            # 例如 "git commit" 匹配 "帮我 git commit"，但不匹配 "git committing"
            pattern = r'\b' + re.escape(keyword_lower) + r'\b'
            if re.search(pattern, message_lower):
                matched_kw.append(keyword)
        
        if matched_kw:
            matched.append({
                "name": skill.name,
                "description": skill.description,
                "when_to_use": getattr(skill, "when_to_use", "") or "",
                "context": getattr(skill, "hook_mode", None) or getattr(skill, "context", "inline"),
                "allowed_tools": skill.allowed_tools,
                "model": skill.model,
                "matched_keywords": matched_kw,
                "skill_dir": skill.skill_dir,
            })
    
    return matched


def format_hook_skill_context(hook_matches: list[dict[str, Any]]) -> str:
    """格式化 hook 匹配的 Skill 为候选提示。

    Hook 匹配只是候选，让模型根据 when_to_use 判断是否调用 skill 工具。

    Args:
        hook_matches: match_skill_hooks 返回的匹配列表

    Returns:
        格式化后的上下文字符串，用于注入到用户消息
    """
    if not hook_matches:
        return ""

    lines = [
        "<skill_hooks>",
        "The following skills were matched by keyword. ",
        "Use a skill only if it directly matches the user's intent; otherwise ignore this block.",
        "To invoke, call the `skill` tool with the skill name.",
    ]

    for idx, match in enumerate(hook_matches, start=1):
        lines.append(f"\n{idx}. {match['name']} (matched: {', '.join(match['matched_keywords'])})")
        if match.get('description'):
            lines.append(f"   {match['description']}")
        if match.get('when_to_use'):
            lines.append(f"   When to use: {match['when_to_use']}")

    lines.append("</skill_hooks>")
    return "\n".join(lines)
