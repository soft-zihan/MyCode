"""Tool call rendering — OpenCode-aligned design with icons and inline/block patterns."""

from __future__ import annotations

from rich.text import Text

from .output import console, _safe_text
from .theme import theme


_TOOL_ICONS = {
    "read_file": "→",
    "write_file": "←",
    "edit_file": "←",
    "list_files": "✱",
    "grep_search": "✱",
    "run_shell": "$",
    "skill": "→",
    "skill_create": "⚙",
    "skill_evolve": "⚙",
    "agent": "│",
    "glob": "✱",
    "webfetch": "%",
    "websearch": "◈",
    "todowrite": "⚙",
}


def _get_tool_icon(name: str) -> str:
    return _TOOL_ICONS.get(name, "⚙")


def _get_tool_summary(name: str, inp: dict) -> str:
    if name == "read_file":
        return inp.get("file_path", "")
    if name == "write_file":
        return inp.get("file_path", "")
    if name == "edit_file":
        return inp.get("file_path", "")
    if name == "list_files":
        return inp.get("pattern", "")
    if name == "grep_search":
        pattern = inp.get("pattern", "")
        path = inp.get("path", ".")
        return f'"{pattern}" in {path}'
    if name == "run_shell":
        cmd = inp.get("command", "")
        return cmd[:60] + "..." if len(cmd) > 60 else cmd
    if name == "skill":
        return inp.get("skill_name", "")
    if name == "skill_create":
        return inp.get("name", "")
    if name == "skill_evolve":
        return inp.get("skill_name", "")
    if name == "agent":
        agent_type = inp.get("type", "general")
        desc = inp.get("description", "")
        return f"{agent_type} — {desc}"
    return f"{name}"


def print_tool_call(name: str, inp: dict) -> None:
    icon = _get_tool_icon(name)
    summary = _get_tool_summary(name, inp)

    icon_text = Text(icon, style=theme.TEXT_MUTED)
    summary_text = Text(_safe_text(summary), style=theme.TEXT)

    line = Text.assemble(icon_text, " ", summary_text)
    console.print(line)


def print_tool_result(name: str, result: str) -> None:
    result = _safe_text(result)

    # 文件变更结果特殊处理
    if (name in ("edit_file", "write_file")) and not result.startswith("Error"):
        _print_file_change_result(name, result)
        return

    # Shell 结果：block 样式（左边框）
    if name == "run_shell":
        _print_shell_result(result)
        return

    # grep/glob：显示匹配计数
    if name in ("grep_search", "list_files"):
        _print_search_result(name, result)
        return

    # 错误结果：红色样式
    if result.startswith("Error") or result.startswith("Denied"):
        _print_error_result(result)
        return

    # 普通结果：截断显示
    max_len = 500
    truncated = result
    if len(result) > max_len:
        truncated = result[:max_len] + f"\n  ... ({len(result)} chars total)"

    console.print(f"[{theme.TEXT_MUTED}]{truncated}[/{theme.TEXT_MUTED}]")


def _print_shell_result(result: str) -> None:
    """Shell 输出：block 样式（左边框）。"""
    lines = result.split("\n")
    # 只显示前 20 行
    display_lines = lines[:20]
    for line in display_lines:
        console.print(f"[{theme.BORDER}]│[/{theme.BORDER}] [{theme.TEXT_MUTED}]{line}[/{theme.TEXT_MUTED}]")
    if len(lines) > 20:
        console.print(f"[{theme.BORDER}]│[/{theme.BORDER}] [{theme.TEXT_MUTED}]... ({len(lines) - 20} more lines)[/{theme.TEXT_MUTED}]")


def _print_search_result(name: str, result: str) -> None:
    """grep/glob 结果：显示匹配计数。"""
    lines = [l for l in result.split("\n") if l.strip()]
    count = len(lines)
    console.print(f"[{theme.SUCCESS}]✓[/{theme.SUCCESS}] [{theme.TEXT_MUTED}]{count} matches[/{theme.TEXT_MUTED}]")
    # 显示前 5 个匹配
    for line in lines[:5]:
        console.print(f"  [{theme.TEXT}]{line}[/{theme.TEXT}]")
    if count > 5:
        console.print(f"  [{theme.TEXT_MUTED}]... and {count - 5} more[/{theme.TEXT_MUTED}]")


def _print_error_result(result: str) -> None:
    """错误结果：红色样式。"""
    lines = result.split("\n")
    for line in lines[:10]:
        console.print(f"[{theme.ERROR}]│[/{theme.ERROR}] [{theme.ERROR}]{line}[/{theme.ERROR}]")
    if len(lines) > 10:
        console.print(f"[{theme.ERROR}]│[/{theme.ERROR}] [{theme.ERROR}]... ({len(lines) - 10} more lines)[/{theme.ERROR}]")


def _print_file_change_result(name: str, result: str) -> None:
    lines = result.split("\n")
    
    # Just show the first line (success message) and diff
    if lines:
        console.print(f"[{theme.SUCCESS}]✓[/{theme.SUCCESS}] [{theme.TEXT_MUTED}]{lines[0]}[/{theme.TEXT_MUTED}]")

    max_display = 40
    content_lines = lines[1:]
    display_lines = content_lines[:max_display]

    for line in display_lines:
        if not line.strip():
            continue
        if line.startswith("@@"):
            console.print(f"[{theme.INFO}]{line}[/{theme.INFO}]")
        elif line.startswith("- "):
            console.print(f"[{theme.ERROR}]{line}[/{theme.ERROR}]")
        elif line.startswith("+ "):
            console.print(f"[{theme.SUCCESS}]{line}[/{theme.SUCCESS}]")
        else:
            console.print(f"[{theme.TEXT_MUTED}]{line}[/{theme.TEXT_MUTED}]")

    if len(content_lines) > max_display:
        console.print(f"[{theme.TEXT_MUTED}]... ({len(content_lines) - max_display} more lines)[/{theme.TEXT_MUTED}]")
