"""Command Palette - Fuzzy search for commands (like opencode's Ctrl+K).

Provides a searchable command palette with:
- Fuzzy matching on command name and description
- Category grouping
- Quick execution
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..tui.dialog import SelectDialog, DialogOption

if TYPE_CHECKING:
    from .registry import Command, CommandRegistry


def _fuzzy_match(query: str, text: str) -> bool:
    """Simple fuzzy match - all query chars must appear in order."""
    query = query.lower()
    text = text.lower()
    qi = 0
    for ch in text:
        if qi < len(query) and ch == query[qi]:
            qi += 1
    return qi == len(query)


def _score_match(query: str, text: str) -> int:
    """Score a fuzzy match (higher is better)."""
    query = query.lower()
    text = text.lower()
    
    # Exact prefix match gets highest score
    if text.startswith(query):
        return 1000
    
    # Exact substring match
    if query in text:
        return 500
    
    # Fuzzy match - score by how compact the match is
    qi = 0
    last_pos = -1
    score = 0
    for i, ch in enumerate(text):
        if qi < len(query) and ch == query[qi]:
            if last_pos >= 0:
                # Penalize gaps
                score -= (i - last_pos - 1) * 10
            score += 100
            last_pos = i
            qi += 1
    
    if qi == len(query):
        return score
    return 0


async def show_command_palette(registry: "CommandRegistry") -> str | None:
    """Show the command palette and return the selected command name.
    
    Args:
        registry: The command registry to search
    
    Returns:
        Selected command name, or None if cancelled
    """
    commands = registry.list_all()
    
    # Build options
    options = []
    for cmd in sorted(commands, key=lambda c: c.name):
        # Format: /name - description
        title = f"/{cmd.name}"
        description = cmd.description or ""
        category = cmd.category or "general"
        
        # Add aliases hint
        if cmd.aliases:
            aliases_str = ", ".join(cmd.aliases[:2])
            description = f"{description} (aliases: {aliases_str})" if description else f"aliases: {aliases_str}"
        
        options.append(DialogOption(
            title=title,
            value=cmd.name,
            description=description,
            category=category,
        ))
    
    dialog = SelectDialog(
        title="Command Palette",
        options=options,
        placeholder="Search commands...",
    )
    
    return await dialog.run_async()


def execute_command_from_palette(agent, cmd_name: str) -> None:
    """Execute a command selected from the palette.
    
    Args:
        agent: The agent instance
        cmd_name: Command name to execute
    """
    from .registry import registry
    
    cmd = registry.get(cmd_name)
    if cmd:
        # Run the command handler
        import asyncio
        result = cmd.handler(agent, "")
        if asyncio.iscoroutine(result):
            asyncio.get_event_loop().run_until_complete(result)
