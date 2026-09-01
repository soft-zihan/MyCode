from __future__ import annotations

from typing import TYPE_CHECKING

from ..registry import command
from ...ui import (
    print_info,
    print_error,
    print_context_rows,
    print_memory_entries,
    print_skill_entries,
)
from ...memory import list_memories
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
    print_context_rows(rows)


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
    name="memory",
    description="List memories or prune",
    usage="[prune [--dry-run] [--threshold=0.1]]",
    category="inspect",
)
async def cmd_memory(agent: "Agent", args: str) -> None:
    if args.startswith("prune"):
        from ..memory import auto_prune_memories
        prune_args = args[5:].strip().split()
        dry_run = "--dry-run" in prune_args
        threshold = 0.1
        for arg in prune_args:
            if arg.startswith("--threshold="):
                try:
                    threshold = float(arg.split("=")[1])
                except ValueError:
                    pass
        pruned = auto_prune_memories(threshold=threshold, dry_run=dry_run)
        if dry_run:
            print_info(f"Would prune {len(pruned)} memories:")
        else:
            print_info(f"Pruned {len(pruned)} memories:")
        for f in pruned[:10]:
            print(f"  - {f}")
        if len(pruned) > 10:
            print(f"  ... and {len(pruned) - 10} more")
        return
    memories = list_memories()
    if not memories:
        print_info("No memories saved yet.")
        return
    print_memory_entries(memories)


@command(
    name="skills",
    description="List available skills",
    category="inspect",
)
async def cmd_skills(agent: "Agent", args: str) -> None:
    skills = discover_skills()
    if not skills:
        print_info("No skills found. Add skills to .bear/skills/<name>/SKILL.md")
        return
    print_skill_entries(skills)


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
    from ..online_skill_eval import format_online_skill_eval_async
    side_query = agent._build_side_query(max_tokens=2400)
    print_info(await format_online_skill_eval_async(side_query=side_query))


@command(
    name="trace",
    description="View or toggle trace logging",
    usage="[on|off|<n>]",
    category="inspect",
)
async def cmd_trace(agent: "Agent", args: str) -> None:
    from ..trace import set_trace_enabled, trace_enabled, recent_events, trace_path
    from ..ui import print_trace_rows

    arg = args.strip()
    if arg == "on":
        set_trace_enabled(True)
        print_info("Trace logging: ON")
    elif arg == "off":
        set_trace_enabled(False)
        print_info("Trace logging: OFF")
    else:
        n = 20
        if arg.isdigit():
            n = max(1, int(arg))
        print_trace_rows(recent_events(n), str(trace_path()), trace_enabled())
