from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

from ..registry import command
from ...ui import print_info, print_error, set_thinking_visible, thinking_visible

if TYPE_CHECKING:
    from ..agent import Agent


@command(
    name="cd",
    description="Change working directory",
    usage="[<path>]",
    category="config",
)
async def cmd_cd(agent: "Agent", args: str) -> None:
    target = args.strip()
    if not target:
        print_info(f"Current directory: {Path.cwd()}")
        return
    new_dir = Path(os.path.expanduser(target))
    if not new_dir.is_absolute():
        new_dir = Path.cwd() / new_dir
    try:
        new_dir = new_dir.resolve()
        if not new_dir.is_dir():
            print_error(f"Not a directory: {new_dir}")
            return
        os.chdir(new_dir)
    except OSError as e:
        print_error(f"Cannot change directory: {e}")
        return
    agent._refresh_runtime_system_prompt()
    print_info(f"Changed working directory to: {new_dir}")


@command(
    name="thinking",
    description="Toggle thinking display",
    category="config",
)
async def cmd_thinking(agent: "Agent", args: str) -> None:
    new_state = not thinking_visible()
    set_thinking_visible(new_state)
    print_info(f"Thinking display: {'ON' if new_state else 'OFF'}")


@command(
    name="plan",
    description="Toggle plan mode",
    category="config",
)
async def cmd_plan(agent: "Agent", args: str) -> None:
    agent.toggle_plan_mode()


@command(
    name="cost",
    description="Show cost information",
    category="config",
)
async def cmd_cost(agent: "Agent", args: str) -> None:
    agent.show_cost()


@command(
    name="help",
    description="Show help message",
    category="config",
)
async def cmd_help(agent: "Agent", args: str) -> None:
    from .registry import registry
    cmds = "\n".join(f"  /{c.name}" for c in registry.list_all())
    print_info(
        "REPL commands:\n" + cmds +
        "\n\nTips:\n"
        "  @path            引用文件/目录，内容自动注入本轮输入\n"
        "  Tab              补全 / 命令与 @ 路径\n"
        "  /help            显示本帮助；完整说明见 --help"
    )


@command(
    name="graph",
    description="Manage code-review-graph",
    usage="build|update|status",
    category="config",
)
async def cmd_graph(agent: "Agent", args: str) -> None:
    from ..graph_cmd import run_graph_command
    from ..ui import console
    console.print(run_graph_command(args.strip()))


@command(
    name="models",
    description="List available models or switch model",
    usage="[model_name]",
    category="config",
)
async def cmd_models(agent: "Agent", args: str) -> None:
    from ...core.model_registry import discover_endpoints
    target = args.strip()
    if target:
        agent.model = target
        print_info(f"Switched to model: {target}")
        return
    
    endpoints = discover_endpoints()
    if not endpoints:
        print_info(f"Current model: {agent.model}\n\nNo additional endpoints configured.\nSet BEAR_ENDPOINT_<ID>_BASE_URL/API_KEY/MODEL to add more.")
        return
    
    # Try interactive model selection
    try:
        from ...tui.dialog import SelectDialog, DialogOption
        
        options = []
        for eid, ep in sorted(endpoints.items()):
            model_name = ep.model or "(default)"
            options.append(DialogOption(
                title=model_name,
                value=eid,
                description=ep.base_url or "",
                category="Endpoints",
            ))
        
        # Add common models as quick options
        common_models = [
            ("claude-sonnet-4-20250514", "Anthropic Claude Sonnet 4"),
            ("claude-opus-4-20250514", "Anthropic Claude Opus 4"),
            ("gpt-4o", "OpenAI GPT-4o"),
            ("gpt-4o-mini", "OpenAI GPT-4o Mini"),
            ("deepseek-chat", "DeepSeek Chat"),
            ("deepseek-v4-pro", "DeepSeek V4 Pro"),
        ]
        
        for model_id, desc in common_models:
            options.append(DialogOption(
                title=model_id,
                value=model_id,
                description=desc,
                category="Quick Switch",
            ))
        
        dialog = SelectDialog(
            title="Switch Model",
            options=options,
            placeholder="Search models...",
        )
        
        selected = await dialog.run_async()
        if selected:
            # Check if it's an endpoint ID or a model name
            if selected in endpoints:
                ep = endpoints[selected]
                agent.model = ep.model or selected
                print_info(f"Switched to endpoint: {selected} (model: {agent.model})")
            else:
                agent.model = selected
                print_info(f"Switched to model: {selected}")
    
    except ImportError:
        # Fallback to table display
        from ...ui import console
        from rich.table import Table
        from rich import box
        table = Table(box=box.SIMPLE, padding=(0, 2))
        table.add_column("Endpoint", style="cyan")
        table.add_column("Model", style="white")
        table.add_column("Base URL", style="dim")
        for eid, ep in sorted(endpoints.items()):
            table.add_row(eid, ep.model or "-", ep.base_url or "-")
        console.print(f"\n[bold]Current model:[/bold] {agent.model}\n")
        console.print(table)
        console.print("\n[dim]Usage: /models <model_name> to switch[/dim]")


@command(
    name="status",
    description="Show current status (model, session, tokens, cost)",
    category="config",
)
async def cmd_status(agent: "Agent", args: str) -> None:
    from ...ui import console
    from rich.table import Table
    from rich import box
    table = Table(box=box.SIMPLE, padding=(0, 2))
    table.add_column("Field", style="cyan")
    table.add_column("Value", style="white")
    cost = agent._get_current_cost_usd()
    budget = f" / ${agent.max_cost_usd}" if agent.max_cost_usd else ""
    turns = f" / {agent.max_turns}" if agent.max_turns else ""
    table.add_row("Model", agent.model)
    table.add_row("Session", agent.session_id)
    table.add_row("Tokens (in/out)", f"{agent.total_input_tokens} / {agent.total_output_tokens}")
    table.add_row("Cost", f"${cost:.4f}{budget}")
    table.add_row("Turns", f"{agent.current_turns}{turns}")
    table.add_row("Permission", agent.permission_mode)
    table.add_row("Working Dir", str(Path.cwd()))
    console.print(table)


@command(
    name="permission",
    description="Switch permission mode globally",
    usage="[mode]",
    aliases=["perm"],
    category="config",
)
async def cmd_permission(agent: "Agent", args: str) -> None:
    from ...ui import print_info, print_error
    from ...permissions import get_permission_set_from_legacy_mode

    mode = args.strip().lower()
    valid_modes = ["default", "acceptEdits", "bypassPermissions", "plan", "dontAsk"]

    if not mode:
        print_info(f"Current permission mode: {agent.permission_mode}\n\nValid modes: {', '.join(valid_modes)}\n\nUsage: /permission <mode>\n\nQuick: /yolo on|off")
        return

    if mode not in valid_modes:
        print_error(f"Invalid mode: {mode}\n\nValid modes: {', '.join(valid_modes)}")
        return

    agent.permission_mode = mode
    agent._permission_set = get_permission_set_from_legacy_mode(mode)
    agent._tool_executor.permission_set = agent._permission_set
    print_info(f"Permission mode switched to: {mode}")


@command(
    name="yolo",
    description="Toggle bypass permissions mode",
    usage="on|off",
    category="config",
)
async def cmd_yolo(agent: "Agent", args: str) -> None:
    from ...ui import print_info, print_error
    from ...permissions import get_permission_set_from_legacy_mode

    arg = args.strip().lower()

    if arg == "on":
        agent.permission_mode = "bypassPermissions"
        agent._permission_set = get_permission_set_from_legacy_mode("bypassPermissions")
        agent._tool_executor.permission_set = agent._permission_set
        print_info("YOLO mode: ON - all permissions bypassed")
    elif arg == "off":
        agent.permission_mode = "default"
        agent._permission_set = get_permission_set_from_legacy_mode("default")
        agent._tool_executor.permission_set = agent._permission_set
        print_info("YOLO mode: OFF - default permissions restored")
    else:
        status = "ON" if agent.permission_mode == "bypassPermissions" else "OFF"
        print_info(f"YOLO mode: {status}\n\nUsage: /yolo on|off")


@command(
    name="palette",
    description="Open command palette (Ctrl+K)",
    aliases=["p"],
    category="config",
)
async def cmd_palette(agent: "Agent", args: str) -> None:
    """Open the command palette for fuzzy search."""
    from ..registry import registry
    from ..palette import show_command_palette, execute_command_from_palette
    
    cmd_name = await show_command_palette(registry)
    if cmd_name:
        # Execute the selected command
        cmd = registry.get(cmd_name)
        if cmd:
            result = cmd.handler(agent, args)
            if hasattr(result, '__await__'):
                await result
