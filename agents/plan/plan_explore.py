"""Plan Explore — 调研、设计空间分析。

Explore 阶段在创建 plan 之前进行调研，帮助用户明确需求。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from agents.core.workspace import get_workspace


@dataclass
class ExploreResult:
    topic: str
    findings: list[str]
    suggested_granularity: str
    suggested_slug: str
    summary: str


def save_explore_result(topic: str, content: str) -> Path:
    from agents.plan.plan_manager import get_plans_dir, _slugify, _git_commit

    plans_dir = get_plans_dir()
    explorations_dir = plans_dir / "_explorations"
    explorations_dir.mkdir(parents=True, exist_ok=True)

    slug = _slugify(topic)
    filepath = explorations_dir / f"{slug}.md"
    filepath.write_text(content)

    _git_commit(f"plan: explore {topic}")
    return filepath


def list_explorations() -> list[Path]:
    from agents.plan.plan_manager import get_plans_dir

    plans_dir = get_plans_dir()
    explorations_dir = plans_dir / "_explorations"

    if not explorations_dir.exists():
        return []

    return list(explorations_dir.glob("*.md"))


EXPLORE_PROMPT = """你是一个技术调研助手。用户想要探索一个主题，但不确定具体要做什么。

请帮助用户：
1. 分析现有代码结构
2. 识别设计空间和可能的方案
3. 评估可行性和风险
4. 建议合适的 plan 粒度（minimal/standard/full）

用户主题：{topic}

请提供：
- 关键发现（3-5 点）
- 建议的 plan slug
- 建议的粒度及理由
- 简短总结

格式要求：
1. 先列出发现
2. 然后给出建议
3. 保持简洁
"""


def build_explore_prompt(topic: str) -> str:
    return EXPLORE_PROMPT.format(topic=topic)
