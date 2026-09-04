from __future__ import annotations

from typing import TYPE_CHECKING

from ..registry import command
from ...logging import print_info, print_error
from ...skills import (
    create_skill,
    evolve_skill,
    execute_skill,
    get_skill_by_name,
    record_feedback,
)

if TYPE_CHECKING:
    from ..agent import Agent


@command(
    name="skill-feedback",
    description="Record feedback for a skill",
    usage="<skill-name> <rating> [note]",
    category="skill",
)
async def cmd_skill_feedback(agent: "Agent", args: str) -> None:
    parts = args.strip().split(" ", 2)
    if len(parts) < 2:
        print_error("Usage: /skill-feedback <skill-name> <rating> [note]")
        return
    note = parts[2] if len(parts) > 2 else ""
    record_feedback(parts[0], parts[1], note)
    print_info(f"Recorded feedback for skill: {parts[0]}")


@command(
    name="skill-evolve",
    description="Evolve a skill with a durable lesson",
    usage="<skill-name> <durable lesson>",
    category="skill",
)
async def cmd_skill_evolve(agent: "Agent", args: str) -> None:
    parts = args.strip().split(" ", 1)
    if len(parts) < 2:
        print_error("Usage: /skill-evolve <skill-name> <durable lesson>")
        return
    result = evolve_skill(parts[0], parts[1], rationale="Manual REPL evolution", target="active")
    if result.get("ok"):
        print_info(f"Evolved skill {result.get('skill')} to version {result.get('version')}")
    else:
        print_error(str(result.get("error") or result))


@command(
    name="skill-create",
    description="Create a new skill",
    usage="<name> | <description> | <when-to-use> | <instructions>",
    category="skill",
)
async def cmd_skill_create(agent: "Agent", args: str) -> None:
    parts = [part.strip() for part in args.split("|", 3)]
    if len(parts) < 4 or not all(parts[:4]):
        print_error("Usage: /skill-create <name> | <description> | <when-to-use> | <instructions>")
        return
    result = create_skill(
        name=parts[0],
        description=parts[1],
        when_to_use=parts[2],
        instructions=parts[3],
        target="project",
        context="inline",
        user_invocable=False,
        evidence="Manual REPL skill creation",
    )
    if result.get("ok"):
        print_info(f"Created skill {result.get('skill')} at {result.get('file')}")
    else:
        print_error(str(result.get("error") or result))


@command(
    name="extract_now",
    description="Run online skill extraction now",
    usage="[hint]",
    category="skill",
)
async def cmd_extract_now(agent: "Agent", args: str) -> None:
    hint = args.strip()
    result = await agent.extract_now(hint)
    if result.get("ok"):
        print_info("Ran online skill extraction for the current pending window.")
    else:
        print_error(str(result.get("error") or result))


async def handle_skill_invocation(agent: "Agent", cmd_name: str, cmd_args: str) -> bool:
    skill = get_skill_by_name(cmd_name)
    if not skill or not skill.user_invocable:
        return False
    print_info(f"Invoking skill: {skill.name}")
    try:
        if skill.context == "fork":
            await agent.chat(f'Use the skill tool to invoke "{skill.name}" with args: {cmd_args or "(none)"}')
        else:
            result = execute_skill(skill.name, cmd_args)
            if not result:
                print_error(f"Unknown skill: {skill.name}")
                return True
            await agent.chat(result["prompt"])
    except Exception as e:
        if "abort" not in str(e).lower():
            print_error(str(e))
    return True
