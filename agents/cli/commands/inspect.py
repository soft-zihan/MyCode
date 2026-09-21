from __future__ import annotations

from typing import TYPE_CHECKING

from ..registry import command
from ...logging import print_info, print_error
from ...skills import discover_skills, skill_stats

if TYPE_CHECKING:
    from ..agent import Agent


@command(
    name="context",
    description="Show current context visualization",
    category="inspect",
)
async def cmd_context(agent: "Agent", args: str) -> None:
    rows = agent.describe_context()
    if not rows:
        print_info("Context is empty.")
        return
    print_info(f"Context: {len(rows)} entries")
    for row in rows:
        print_info(f"  {row}")


@command(
    name="ctx",
    description="Delete or keep context messages",
    usage="del|keep <spec>",
    category="inspect",
)
async def cmd_ctx(agent: "Agent", args: str) -> None:
    from ..context_edit import parse_index_spec

    parts = args.split(None, 1)
    if len(parts) < 2 or parts[0] not in ("del", "keep"):
        print_error("Usage: /ctx del|keep <index> [index2 ...]  e.g. /ctx del 1,3,5~10")
        return
    action = parts[0]
    try:
        indexes = parse_index_spec(parts[1])
    except ValueError:
        print_error(f"Usage: /ctx {action} <index> [index2 ...]  e.g. /ctx {action} 1,3,5~10")
        return
    if not indexes:
        print_error(f"Usage: /ctx {action} <index> [index2 ...]  e.g. /ctx {action} 1,3,5~10")
        return
    if action == "del":
        print_info(agent.delete_context_messages(indexes))
    else:
        print_info(agent.keep_context_messages(indexes))


@command(
    name="skills",
    description="List available skills",
    category="inspect",
)
async def cmd_skills(agent: "Agent", args: str) -> None:
    skills = discover_skills()
    if not skills:
        print_info("No skills found. Add skills to .mycode/skills/<name>/SKILL.md")
        return
    print_info(f"Skills: {len(skills)} entries")
    for s in skills:
        print_info(f"  {s}")


@command(
    name="skill-stats",
    description="Show skill usage statistics",
    category="inspect",
)
async def cmd_skill_stats(agent: "Agent", args: str) -> None:
    print_info(skill_stats())


@command(
    name="skill-eval",
    description="Run online skill evaluation",
    category="inspect",
)
async def cmd_skill_eval(agent: "Agent", args: str) -> None:
    from ...skills.eval_report import format_online_skill_eval_async
    side_query = agent._build_side_query(max_tokens=2400)
    print_info(await format_online_skill_eval_async(side_query=side_query))


@command(
    name="trace",
    description="View trace logging status",
    usage="",
    category="inspect",
)
async def cmd_trace(agent: "Agent", args: str) -> None:
    from ...observability.trace import tracing_enabled

    print_info(f"Trace: Langfuse={'ON' if tracing_enabled() else 'OFF'}")
    print_info("Traces are exported with the official Langfuse SDK.")
    print_info("Set MYCODE_TRACING=1 to enable, MYCODE_TRACING=0 to disable.")
