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


# 从文件加载 side query 提示词
from pathlib import Path
_PROMPTS_DIR = Path(__file__).parent.parent / "prompts" / "side_query"
EXPLORE_PROMPT = (_PROMPTS_DIR / "explore.txt").read_text(encoding="utf-8")


def build_explore_prompt(topic: str) -> str:
    return EXPLORE_PROMPT.format(topic=topic)
