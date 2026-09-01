from __future__ import annotations

from typing import TYPE_CHECKING

from ..registry import command
from ...ui import print_info, print_error

if TYPE_CHECKING:
    from ...agent import Agent


@command(
    name="goal",
    description="Autonomous goal mode with verifier loop",
    usage="<goal description>",
    category="agent",
)
async def cmd_goal(agent: "Agent", args: str) -> None:
    from ...goal import GoalLoop, extract_goal_criteria

    goal_text = args.strip()
    if not goal_text:
        print_error("Usage: /goal <goal description>")
        return
    side_query = agent._build_side_query(max_tokens=2400)
    if not side_query:
        print_error("No side-query model configured; cannot extract goal criteria.")
        return
    print_info("Extracting success criteria...")
    criteria = await extract_goal_criteria(goal_text, side_query)
    if not criteria:
        print_error("Could not extract verifiable success criteria. Try a more concrete goal.")
        return
    print_info("Success criteria:\n" + "\n".join(f"  {i}. {c}" for i, c in enumerate(criteria, 1)))
    try:
        answer = input("  Start goal mode with these criteria? (y/n): ")
    except EOFError:
        answer = "n"
    if not answer.lower().startswith("y"):
        print_info("Goal mode cancelled.")
        return
    loop = GoalLoop(agent, goal_text, criteria, side_query=side_query)
    state = await loop.run()
    print_info(
        f"Goal mode finished: {state.status} after {state.iteration} iteration(s)."
        + ("\nUse /rewind to undo changes if the result is not what you wanted."
           if state.status != "achieved" else "")
    )
