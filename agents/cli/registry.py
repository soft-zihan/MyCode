from __future__ import annotations

from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional, Union

CommandHandler = Callable[..., Optional[Awaitable[None]]]


@dataclass
class Command:
    name: str
    handler: CommandHandler
    description: str = ""
    usage: str = ""
    aliases: list[str] = field(default_factory=list)
    category: str = "general"


class CommandRegistry:
    def __init__(self):
        self._commands: dict[str, Command] = {}
        self._aliases: dict[str, str] = {}

    def register(self, cmd: Command) -> None:
        self._commands[cmd.name] = cmd
        for alias in cmd.aliases:
            self._aliases[alias] = cmd.name

    def get(self, name: str) -> Command | None:
        if name in self._commands:
            return self._commands[name]
        if name in self._aliases:
            return self._commands.get(self._aliases[name])
        return None

    def find(self, text: str) -> tuple[Command | None, str]:
        if not text.startswith("/"):
            return None, text
        parts = text[1:].split(None, 1)
        cmd_name = parts[0]
        args = parts[1] if len(parts) > 1 else ""
        cmd = self.get(cmd_name)
        return cmd, args

    def list_by_category(self) -> dict[str, list[Command]]:
        result: dict[str, list[Command]] = {}
        for cmd in self._commands.values():
            result.setdefault(cmd.category, []).append(cmd)
        return result

    def list_all(self) -> list[Command]:
        return list(self._commands.values())

    def format_help(self, category: str | None = None) -> str:
        cmds = self.list_by_category()
        if category:
            cmds = {category: cmds.get(category, [])}
        lines = []
        for cat, commands in sorted(cmds.items()):
            lines.append(f"\n[{cat}]")
            for cmd in sorted(commands, key=lambda c: c.name):
                desc = cmd.description or ""
                usage = cmd.usage or ""
                if usage:
                    lines.append(f"  /{cmd.name} {usage}  -  {desc}")
                else:
                    lines.append(f"  /{cmd.name}  -  {desc}")
        return "\n".join(lines)


registry = CommandRegistry()


def command(
    name: str,
    description: str = "",
    usage: str = "",
    aliases: list[str] | None = None,
    category: str = "general",
):
    def decorator(func: CommandHandler) -> CommandHandler:
        cmd = Command(
            name=name,
            handler=func,
            description=description,
            usage=usage,
            aliases=aliases or [],
            category=category,
        )
        registry.register(cmd)
        return func
    return decorator
