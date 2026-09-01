"""Terminal UI rendering — OpenCode-aligned design."""

from __future__ import annotations

import os
import sys

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.text import Text
from rich.table import Table
from rich.align import Align

from .theme import theme

console = Console(highlight=False)


def _safe_text(value: object) -> str:
    return str(value).encode("utf-8", errors="replace").decode("utf-8")


def _safe_stdout_write(text: object) -> None:
    sys.stdout.write(_safe_text(text))
    sys.stdout.flush()


COOKIE_BEAR = r"""
        _     _
      _( )___( )_
     /  o     o  \
    |      ^      |
    |   \_____/   |
     \  .-.-.    /
      '-.....---'
"""

LOGO = r"""
  ███╗   ███╗██╗   ██╗
  ████╗ ████║╚██╗ ██╔╝
  ██╔████╔██║ ╚████╔╝
  ██║╚██╔╝██║  ╚██╔╝
  ██║ ╚═╝ ██║   ██║
  ╚═╝     ╚═╝   ╚═╝
"""


def print_welcome() -> None:
    title = Text("BearCode", style=f"bold {theme.PRIMARY}")
    subtitle = Text("Evolvable Coding Agent", style=f"italic {theme.TEXT_MUTED}")

    commands = Table.grid(padding=(0, 1))
    commands.add_column(style=f"bold {theme.SECONDARY}", no_wrap=True)
    commands.add_column(style=theme.TEXT_MUTED)
    commands.add_row("/new", "start new session")
    commands.add_row("/models", "list or switch models")
    commands.add_row("/status", "show current status")
    commands.add_row("/permission", "switch permission mode")
    commands.add_row("/yolo on|off", "quick toggle bypass permissions")
    commands.add_row("/plan", "read-only planning")
    commands.add_row("/goal", "autonomous goal mode")
    commands.add_row("/context", "visualize context")
    commands.add_row("/rewind", "rewind turns + restore files")
    commands.add_row("/fork", "fork session branch")
    commands.add_row("/sessions", "list saved sessions")
    commands.add_row("/compact", "compact context")
    commands.add_row("/thinking", "toggle thinking display")
    commands.add_row("/memory", "list long-term memories")
    commands.add_row("/skills", "list reusable skills")
    commands.add_row("/help", "full command list")
    commands.add_row("!cmd", "run shell directly")
    commands.add_row("/q, exit", "quit")

    console.print()
    console.print(Align.center(title))
    console.print(Align.center(subtitle))
    console.print()
    console.print(Panel(commands, border_style=theme.BORDER, box=box.SIMPLE, padding=(0, 1)))
    console.print()


def print_user_prompt(status: str = "") -> None:
    """显示输入提示行，带底部状态栏。"""
    from pathlib import Path
    import os
    
    # 底部状态栏：cwd 左，status 右
    cwd = str(Path.cwd())
    home = str(Path.home())
    if cwd.startswith(home):
        cwd = "~" + cwd[len(home):]
    
    # 如果路径太长，截断显示
    term_width = console.width or 80
    max_cwd_len = term_width // 2
    if len(cwd) > max_cwd_len:
        cwd = "..." + cwd[-(max_cwd_len-3):]
    
    # 计算填充空格使 status 右对齐
    right_part = status
    padding = max(2, term_width - len(cwd) - len(right_part))
    
    console.print(f"[{theme.TEXT_MUTED}]{cwd}[/{theme.TEXT_MUTED}]" + 
                  " " * padding + 
                  f"[{theme.TEXT_MUTED}]{right_part}[/{theme.TEXT_MUTED}]")
    
    # 输入提示符
    console.print(f"[bold {theme.PRIMARY}]❯[/bold {theme.PRIMARY}] ", end="")


def print_input_metadata(mode: str, model: str) -> None:
    """显示输入区元数据行（mode · model）。"""
    console.print(f"[{theme.TEXT_MUTED}]{mode} · {model}[/{theme.TEXT_MUTED}]")


def print_user_message(text: str, files: list[str] | None = None) -> None:
    """显示用户消息（左边框样式，类似 opencode）。"""
    # 显示消息文本
    for line in text.split("\n"):
        console.print(f"[{theme.PRIMARY}]┃[/{theme.PRIMARY}] {line}")
    
    # 显示文件引用 badge
    if files:
        for f in files:
            console.print(f"[{theme.PRIMARY}]┃[/{theme.PRIMARY}] [{theme.SECONDARY}] File [/{theme.SECONDARY}] {f}")


def print_user_message(text: str, files: list[str] | None = None) -> None:
    """渲染用户消息，带左边框。"""
    lines = text.split("\n")
    console.print()
    for i, line in enumerate(lines):
        if i == 0:
            prefix = f"[{theme.PRIMARY}]┃[/{theme.PRIMARY}] "
        else:
            prefix = f"[{theme.PRIMARY}]┃[/{theme.PRIMARY}] "
        console.print(f"{prefix}[{theme.TEXT}]{_safe_text(line)}[/{theme.TEXT}]")
    if files:
        for f in files:
            console.print(f"  [{theme.SECONDARY}]File[/{theme.SECONDARY}] [{theme.TEXT_MUTED}]{_safe_text(f)}[/{theme.TEXT_MUTED}]")
    console.print()


def print_assistant_text(text: str) -> None:
    _safe_stdout_write(text)


def print_assistant_footer(model: str, duration_s: float, mode: str = "build") -> None:
    icon = Text("▣", style=theme.PRIMARY)
    mode_text = Text(mode, style=theme.TEXT)
    sep = Text(" · ", style=theme.TEXT_MUTED)
    model_text = Text(model, style=theme.TEXT_MUTED)
    dur_text = Text(f"{duration_s:.1f}s", style=theme.TEXT_MUTED)
    line = Text.assemble(icon, " ", mode_text, sep, model_text, sep, dur_text)
    console.print()
    console.print(line, justify="left")


def print_error(msg: str) -> None:
    """错误消息，红色左边框。"""
    lines = _safe_text(msg).split("\n")
    for line in lines:
        console.print(f"[{theme.ERROR}]│[/{theme.ERROR}] [{theme.ERROR}]{line}[/{theme.ERROR}]")


def print_confirmation(command: str) -> None:
    """渲染权限确认对话框，带选项按钮。"""
    console.print()
    console.print(f"[{theme.WARNING}]│[/{theme.WARNING}]")
    console.print(f"[{theme.WARNING}]│[/{theme.WARNING}] [bold {theme.WARNING}]△ Permission required[/bold {theme.WARNING}]")
    console.print(f"[{theme.WARNING}]│[/{theme.WARNING}] [{theme.TEXT_MUTED}]$ {_safe_text(command)}[/{theme.TEXT_MUTED}]")
    console.print(f"[{theme.WARNING}]│[/{theme.WARNING}]")
    console.print(f"[{theme.WARNING}]│[/{theme.WARNING}] [{theme.WARNING}]Allow once[/{theme.WARNING}]  [{theme.TEXT_MUTED}]Allow always[/{theme.TEXT_MUTED}]  [{theme.ERROR}]Reject[/{theme.ERROR}]")
    console.print(f"[{theme.WARNING}]│[/{theme.WARNING}] [{theme.TEXT_MUTED}](y/n/a):[/{theme.TEXT_MUTED}] ", end="")


def print_divider() -> None:
    console.rule(style=theme.BORDER)


def print_cost(input_tokens: int, output_tokens: int) -> None:
    cost_in = (input_tokens / 1_000_000) * 3
    cost_out = (output_tokens / 1_000_000) * 15
    total = cost_in + cost_out
    ctx = f"{input_tokens} tokens"
    cost = f"${total:.4f}"
    console.print(f"[{theme.TEXT_MUTED}]context: {ctx} · cost: {cost}[/{theme.TEXT_MUTED}]")


def print_retry(attempt: int, max_retries: int, reason: str) -> None:
    console.print(f"[{theme.WARNING}]│[/{theme.WARNING}] [{theme.WARNING}]↻ Retry {attempt}/{max_retries}: {reason}[/{theme.WARNING}]")


def print_info(msg: str) -> None:
    """信息消息，青色左边框。"""
    lines = _safe_text(msg).split("\n")
    for line in lines:
        console.print(f"[{theme.INFO}]│[/{theme.INFO}] [{theme.INFO}]{line}[/{theme.INFO}]")


def print_warning(msg: str) -> None:
    """警告消息，黄色左边框。"""
    lines = _safe_text(msg).split("\n")
    for line in lines:
        console.print(f"[{theme.WARNING}]│[/{theme.WARNING}] [{theme.WARNING}]{line}[/{theme.WARNING}]")


def print_goodbye() -> None:
    console.print(f"[{theme.PRIMARY}]│[/{theme.PRIMARY}] [{theme.PRIMARY}]Bye.[/{theme.PRIMARY}]")


def print_interrupted() -> None:
    console.print(f"[{theme.WARNING}]│[/{theme.WARNING}] [{theme.WARNING}]Interrupted. Press Ctrl+C again to exit.[/{theme.WARNING}]")


def print_processing_status() -> None:
    """渲染处理中状态，显示 esc interrupt。"""
    console.print(f"[{theme.TEXT_MUTED}]esc[/{theme.TEXT_MUTED}] [{theme.TEXT}]interrupt[/{theme.TEXT}]")


def print_plan_for_approval(plan_content: str) -> None:
    lines = plan_content.split("\n")
    max_lines = 60
    preview = "\n".join(lines[:max_lines])
    if len(lines) > max_lines:
        preview += f"\n\n... ({len(lines) - max_lines} more lines)"
    console.print(Panel(
        _safe_text(preview),
        title=f"[bold {theme.SECONDARY}]Plan for Approval[/bold {theme.SECONDARY}]",
        border_style=theme.SECONDARY,
        box=box.SIMPLE,
        padding=(1, 2),
    ))


def print_plan_approval_options() -> None:
    table = Table(box=box.SIMPLE, show_header=False, padding=(0, 1))
    table.add_column("choice", style=f"bold {theme.WARNING}", no_wrap=True)
    table.add_column("action", style=theme.TEXT)
    table.add_column("detail", style=theme.TEXT_MUTED)
    table.add_row("1", "Clear context and execute", "fresh start with auto-accept edits")
    table.add_row("2", "Execute", "keep context, auto-accept edits")
    table.add_row("3", "Manually approve edits", "keep context, confirm each edit")
    table.add_row("4", "Keep planning", "provide feedback to revise")
    console.print(table)


def print_sub_agent_start(agent_type: str, description: str) -> None:
    """子 Agent 启动，紫色左边框（类似 opencode）。"""
    console.print(f"[{theme.ACCENT}]│[/{theme.ACCENT}] [{theme.ACCENT}]Agent Task[/{theme.ACCENT}] — {_safe_text(description)}")


def print_sub_agent_end(agent_type: str, _description: str) -> None:
    """子 Agent 完成。"""
    console.print(f"[{theme.SUCCESS}]✓[/{theme.SUCCESS}] [{theme.TEXT_MUTED}]Agent {agent_type} completed[/{theme.TEXT_MUTED}]")


def print_subagent_progress(tasks: list) -> None:
    """显示子智能体进度列表（类似 opencode 的 view subagents）。"""
    from .subagent_tracker import SubagentStatus
    
    if not tasks:
        print_info("No subagent tasks.")
        return
    
    console.print()
    console.print(f"[{theme.PRIMARY}]Subagent Tasks[/{theme.PRIMARY}]")
    console.print()
    
    for task in tasks:
        # Status indicator
        if task.status == SubagentStatus.RUNNING:
            status_icon = f"[{theme.ACCENT}]●[/{theme.ACCENT}]"
            status_text = f"[{theme.ACCENT}]running[/{theme.ACCENT}]"
        elif task.status == SubagentStatus.COMPLETED:
            status_icon = f"[{theme.SUCCESS}]✓[/{theme.SUCCESS}]"
            status_text = f"[{theme.SUCCESS}]completed[/{theme.SUCCESS}]"
        elif task.status == SubagentStatus.FAILED:
            status_icon = f"[{theme.ERROR}]✗[/{theme.ERROR}]"
            status_text = f"[{theme.ERROR}]failed[/{theme.ERROR}]"
        else:  # ABORTED
            status_icon = f"[{theme.WARNING}]⊘[/{theme.WARNING}]"
            status_text = f"[{theme.WARNING}]aborted[/{theme.WARNING}]"
        
        # Duration
        duration = f"{task.elapsed_ms / 1000:.1f}s"
        
        # Tool count
        tool_info = f"{task.tool_calls} tools" if task.tool_calls else ""
        
        # Last tool
        last_tool = f" → {task.last_tool}" if task.last_tool else ""
        
        # Print task line
        console.print(
            f"  {status_icon} [{theme.TEXT}]{task.agent_type}[/{theme.TEXT}] "
            f"[{theme.TEXT_MUTED}]{_safe_text(task.description)}[/{theme.TEXT_MUTED}] "
            f"[{theme.TEXT_MUTED}]{duration}[/{theme.TEXT_MUTED}] "
            f"[{theme.TEXT_MUTED}]{tool_info}{last_tool}[/{theme.TEXT_MUTED}]"
        )
        
        # Show error if failed
        if task.error:
            console.print(f"    [{theme.ERROR}]{_safe_text(task.error)}[/{theme.ERROR}]")
    
    console.print()
