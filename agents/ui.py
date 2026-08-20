"""Terminal UI rendering — colored output, spinner, tool display."""

from __future__ import annotations

import os
import re
import sys
import threading
import time
import json
from pathlib import Path

from rich import box
from rich.align import Align
from rich.cells import cell_len
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

console = Console(highlight=False)

# ─── Basic output ──────────────────────────────────────────


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


def print_welcome() -> None:
    title = Text("My Code", style="bold #f6c177")
    subtitle = Text("Evolvable Coding Agent CLI", style="bold cyan")
    cookie = Text(COOKIE_BEAR, style="bold #d19a66")

    commands = Table.grid(padding=(0, 2))
    commands.add_column(style="bold cyan", no_wrap=True)
    commands.add_column(style="dim")
    commands.add_row("/plan", "read-only planning workflow")
    commands.add_row("/goal", "autonomous goal mode with verifier")
    commands.add_row("/context", "visualize context messages")
    commands.add_row("/ctx del/keep", "delete/keep message groups")
    commands.add_row("/rewind", "rewind turns + restore files")
    commands.add_row("/fork", "fork current session (branch)")
    commands.add_row("/sessions", "list saved sessions")
    commands.add_row("/switch", "switch to another session")
    commands.add_row("/cd", "change working directory")
    commands.add_row("/compact", "compact current context")
    commands.add_row("/thinking", "toggle thinking display")
    commands.add_row("/trace", "view/toggle trace event log")
    commands.add_row("/memory", "list long-term memories")
    commands.add_row("/skills", "list reusable skills")
    commands.add_row("/help", "full command list")
    commands.add_row("!cmd", "run shell command directly")
    commands.add_row("exit", "quit the session")

    body = Table.grid()
    body.add_row(Align.center(cookie))
    body.add_row(Align.center(title))
    body.add_row(Align.center(subtitle))
    body.add_row("")
    body.add_row(Panel(commands, title="Quick Commands", border_style="cyan", box=box.ROUNDED))

    console.print()
    console.print(Panel(
        body,
        title="[bold #f6c177] my code ready [/bold #f6c177]",
        subtitle="[dim]Type your request below[/dim]",
        border_style="#d19a66",
        box=box.ROUNDED,
        padding=(1, 2),
    ))
    console.print()


def print_user_prompt(status: str = "") -> None:
    if status:
        console.print(f"\n[dim]{_safe_text(status)}[/dim]", end="")
    console.print("\n[bold #f6c177]My[/bold #f6c177][bold cyan]Code[/bold cyan] [dim]❯[/dim] ", end="")


def print_assistant_text(text: str) -> None:
    _safe_stdout_write(text)


# ─── thinking 滑动窗口显示 ─────────────────────────────────
# 只保留最近 N 行 thinking，避免超长 thinking 刷屏翻不回去。
# 实现：累积增量文本 → 按行切分 → 只渲染最后 N 行 → 每次增量到来时
# 用 ANSI 转义擦除已绘制的行再重绘，形成"可滑动窗口"效果。

_THINKING_WINDOW_LINES = 8
_thinking_buffer = ""
_thinking_drawn_rows = 0
_thinking_started = False


def print_thinking_text(text: str) -> None:
    """以暗色斜体渲染模型 thinking 内容，滑动窗口只显示最近 N 行。

    不进入任何缓冲区（不影响上下文、不影响助手回复文本）。

    关键点：擦除行数必须按【物理行】计算——长 thinking 行会在终端自动
    折行，若按逻辑行数上移会擦不干净，导致旧行残留、越堆越多。
    """
    global _thinking_buffer, _thinking_drawn_rows, _thinking_started
    _thinking_buffer += text
    lines = _thinking_buffer.split("\n")[-_THINKING_WINDOW_LINES:]
    width = max(console.width, 20)
    rendered = [f"  [thinking] {line}" for line in lines]
    # 每条渲染行占用的物理行数（向上取整，至少 1 行）
    phys_rows = sum(max(1, -(-cell_len(r) // width)) for r in rendered)
    # 擦除上一次绘制的物理行
    if _thinking_drawn_rows > 0:
        _safe_stdout_write(f"\033[{_thinking_drawn_rows}A\r\033[J")
    elif not _thinking_started:
        # 首次绘制前先换行，与 spinner/正文分隔（该分隔行不参与擦除）
        _safe_stdout_write("\n")
        _thinking_started = True
    for r in rendered:
        console.print(Text(r, style="dim italic"))
    _thinking_drawn_rows = phys_rows


def reset_thinking_window() -> None:
    """每轮新响应开始时重置滑动窗口。"""
    global _thinking_buffer, _thinking_drawn_rows, _thinking_started
    _thinking_buffer = ""
    _thinking_drawn_rows = 0
    _thinking_started = False


# ─── thinking 显示开关 ──────────────────────────────────────
# 业界惯例（Claude Code / Codex CLI）：thinking 不进上下文、默认不展示。
# BearCode 同样默认关闭显示，用 /thinking 命令按需打开。

_show_thinking = False


def set_thinking_visible(value: bool) -> None:
    global _show_thinking
    _show_thinking = bool(value)


def thinking_visible() -> bool:
    return _show_thinking


# ─── 流式结束后自动渲染 Markdown ────────────────────────────
# 思路：流式输出时照常逐字打印（同时记录文本与占用的终端行数），
# 本轮最终回复结束后，用 ANSI 转义擦除原文区域，再用 rich.Markdown
# 重渲染。只在 stdout 是 TTY 且文本确实含 Markdown 特征时执行。

_MD_FEATURES = re.compile(
    r"```|^#{1,6}\s|\n\s*[-*+]\s|\n\s*\d+\.\s|\*\*|^\|.+\|\s*$|\[.+\]\(.+\)",
    re.MULTILINE,
)

_md_text = ""
_md_rows = 0
_md_col = 0


def md_render_enabled() -> bool:
    """BEAR_MD_RENDER=0 可关闭；非 TTY（管道/重定向）也关闭。"""
    if os.environ.get("BEAR_MD_RENDER", "").strip() == "0":
        return False
    try:
        return sys.stdout.isatty()
    except Exception:
        return False


def md_looks_renderable(text: str) -> bool:
    """文本足够长且含 Markdown 特征时才值得重渲染。"""
    return len(text.strip()) >= 40 and bool(_MD_FEATURES.search(text))


def _md_count_rows(text: str, col: int, width: int) -> tuple[int, int]:
    """计算把 text 写到 (当前行, col) 之后新增的行数与结束列。

    考虑终端自动换行（按显示宽度 cell_len，CJK 全角字符算 2 列）。
    """
    rows = 0
    for i, seg in enumerate(text.split("\n")):
        if i > 0:
            rows += 1
            col = 0
        if seg:
            cells = cell_len(seg)
            total = col + cells
            if total > 0:
                rows += (total - 1) // width
            col = total % width
    return rows, col


def md_track_begin() -> None:
    """每轮模型流式响应开始时重置跟踪状态。"""
    global _md_text, _md_rows, _md_col
    _md_text, _md_rows, _md_col = "", 0, 0


def md_track_feed(text: str) -> None:
    """流式片段与打印同步喂入，累计文本和占用行数。"""
    global _md_text, _md_rows, _md_col
    _md_text += text
    width = max(console.width, 20)
    added, _md_col = _md_count_rows(text, _md_col, width)
    _md_rows += added


def md_track_flush() -> None:
    """本轮最终回复结束：擦除原始流式文本，重渲染为 Markdown。

    不满足条件（非 TTY / 无 Markdown 特征 / 被禁用）时保持原文不动。
    """
    global _md_text, _md_rows, _md_col
    text, rows = _md_text, _md_rows
    _md_text, _md_rows, _md_col = "", 0, 0
    if not text or not md_render_enabled() or not md_looks_renderable(text):
        return
    if rows > 0:
        _safe_stdout_write(f"\033[{rows}A")
    _safe_stdout_write("\r\033[J")
    from rich.markdown import Markdown

    console.print(Markdown(_safe_text(text)))


def print_tool_call(name: str, inp: dict) -> None:
    icon = _get_tool_icon(name)
    summary = _get_tool_summary(name, inp)
    table = Table.grid(padding=(0, 1))
    table.add_column(style="bold yellow", no_wrap=True)
    table.add_column(style="white")
    table.add_row("tool", f"{icon} {name}")
    if summary:
        table.add_row("input", _safe_text(summary))
    console.print(Panel(
        table,
        title="[bold yellow]Tool Call[/bold yellow]",
        border_style="yellow",
        box=box.ROUNDED,
        padding=(0, 1),
    ))


def print_tool_result(name: str, result: str) -> None:
    result = _safe_text(result)
    if (name in ("edit_file", "write_file")) and not result.startswith("Error"):
        _print_file_change_result(name, result)
        return
    max_len = 500
    truncated = result
    if len(result) > max_len:
        truncated = result[:max_len] + f"\n  ... ({len(result)} chars total)"
    console.print(Panel(
        _safe_text(truncated),
        title=f"[dim]{name} result[/dim]",
        border_style="dim",
        box=box.ROUNDED,
        padding=(0, 1),
    ))


def _print_file_change_result(_name: str, result: str) -> None:
    lines = result.split("\n")
    console.print(Panel(
        _safe_text(lines[0]),
        title="[bold green]File Change[/bold green]",
        border_style="green",
        box=box.ROUNDED,
        padding=(0, 1),
    ))

    max_display = 40
    content_lines = lines[1:]
    display_lines = content_lines[:max_display]

    for line in display_lines:
        if not line.strip():
            continue
        if line.startswith("@@"):
            console.print(f"[cyan]  {line}[/cyan]")
        elif line.startswith("- "):
            console.print(f"[red]  {line}[/red]")
        elif line.startswith("+ "):
            console.print(f"[green]  {line}[/green]")
        else:
            console.print(f"[dim]  {line}[/dim]")
    if len(content_lines) > max_display:
        console.print(f"[dim]  ... ({len(content_lines) - max_display} more lines)[/dim]")


def print_error(msg: str) -> None:
    console.print(Panel(
        _safe_text(msg),
        title="[bold red]Error[/bold red]",
        border_style="red",
        box=box.ROUNDED,
        padding=(0, 1),
    ))


def print_confirmation(command: str) -> None:
    console.print(Panel(
        _safe_text(command),
        title="[bold yellow]Dangerous command[/bold yellow]",
        border_style="yellow",
        box=box.ROUNDED,
        padding=(0, 1),
    ))


def print_divider() -> None:
    console.rule("[dim]turn complete[/dim]", style="dim")


def print_cost(input_tokens: int, output_tokens: int) -> None:
    cost_in = (input_tokens / 1_000_000) * 3
    cost_out = (output_tokens / 1_000_000) * 15
    total = cost_in + cost_out
    table = Table.grid(padding=(0, 2))
    table.add_column(style="cyan")
    table.add_column(style="white")
    table.add_row("input", f"{input_tokens} tokens")
    table.add_row("output", f"{output_tokens} tokens")
    table.add_row("estimate", f"${total:.4f}")
    console.print(Panel(table, title="Cost", border_style="cyan", box=box.ROUNDED))


def print_retry(attempt: int, max_retries: int, reason: str) -> None:
    console.print(_safe_text(f"\n  [yellow]↻ Retry {attempt}/{max_retries}: {reason}[/yellow]"))


def print_info(msg: str) -> None:
    console.print(Panel(
        _safe_text(msg),
        title="[bold cyan]Info[/bold cyan]",
        border_style="cyan",
        box=box.ROUNDED,
        padding=(0, 1),
    ))


def print_warning(msg: str) -> None:
    console.print(Panel(
        _safe_text(msg),
        title="[bold yellow]Notice[/bold yellow]",
        border_style="yellow",
        box=box.ROUNDED,
        padding=(0, 1),
    ))


def print_goodbye() -> None:
    console.print(Panel(
        Text("Bye. Bear cookie saved for next time.", style="bold #f6c177"),
        border_style="#d19a66",
        box=box.ROUNDED,
        padding=(0, 1),
    ))


def print_interrupted() -> None:
    print_warning("Interrupted. Press Ctrl+C again to exit.")


# ─── Spinner ──────────────────────────────────────────────

SPINNER_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

_spinner_thread: threading.Thread | None = None
_spinner_stop = threading.Event()


def start_spinner(label: str = "Thinking") -> None:
    global _spinner_thread
    if _spinner_thread is not None:
        return
    _spinner_stop.clear()

    def _run() -> None:
        frame = 0
        _safe_stdout_write(f"\n  {SPINNER_FRAMES[0]} {label}...")
        while not _spinner_stop.is_set():
            time.sleep(0.08)
            frame = (frame + 1) % len(SPINNER_FRAMES)
            _safe_stdout_write(f"\r  {SPINNER_FRAMES[frame]} {label}...")

    _spinner_thread = threading.Thread(target=_run, daemon=True)
    _spinner_thread.start()


def stop_spinner() -> None:
    global _spinner_thread
    if _spinner_thread is None:
        return
    _spinner_stop.set()
    _spinner_thread.join(timeout=1)
    _spinner_thread = None
    _safe_stdout_write("\r\033[K")


# ─── Plan approval display ──────────────────────────────────


def print_plan_for_approval(plan_content: str) -> None:
    lines = plan_content.split("\n")
    max_lines = 60
    preview = "\n".join(lines[:max_lines])
    if len(lines) > max_lines:
        preview += f"\n\n... ({len(lines) - max_lines} more lines)"
    console.print(Panel(
        _safe_text(preview),
        title="[bold cyan]Plan for Approval[/bold cyan]",
        border_style="cyan",
        box=box.ROUNDED,
        padding=(1, 2),
    ))


def print_plan_approval_options() -> None:
    table = Table(box=box.SIMPLE, show_header=False, padding=(0, 1))
    table.add_column("choice", style="bold yellow", no_wrap=True)
    table.add_column("action", style="white")
    table.add_column("detail", style="dim")
    table.add_row("1", "Clear context and execute", "fresh start with auto-accept edits")
    table.add_row("2", "Execute", "keep context, auto-accept edits")
    table.add_row("3", "Manually approve edits", "keep context, confirm each edit")
    table.add_row("4", "Keep planning", "provide feedback to revise")
    console.print(Panel(table, title="[bold yellow]Choose an option[/bold yellow]", border_style="yellow", box=box.ROUNDED))


# ─── Sub-agent display ──────────────────────────────────────


def print_sub_agent_start(agent_type: str, description: str) -> None:
    console.print(Panel(
        _safe_text(description),
        title=f"[bold magenta]Sub-agent started: {agent_type}[/bold magenta]",
        border_style="magenta",
        box=box.ROUNDED,
        padding=(0, 1),
    ))


def print_sub_agent_end(agent_type: str, _description: str) -> None:
    console.print(Panel(
        "completed",
        title=f"[bold magenta]Sub-agent finished: {agent_type}[/bold magenta]",
        border_style="magenta",
        box=box.ROUNDED,
        padding=(0, 1),
    ))


def print_context_rows(rows: list[dict]) -> None:
    """渲染 /context 表格：index / role / label / tokens（统一 token 计数）。"""
    table = Table(box=box.ROUNDED, header_style="bold cyan", border_style="cyan")
    table.add_column("#", style="bold #f6c177", no_wrap=True)
    table.add_column("Role", style="cyan", no_wrap=True)
    table.add_column("Content", style="white")
    table.add_column("Tokens", style="dim", justify="right", no_wrap=True)
    total = 0
    for r in rows:
        tokens = int(r.get("tokens", 0))
        total += tokens
        table.add_row(
            str(r.get("index", "")),
            _safe_text(r.get("role", "")),
            _safe_text(r.get("label", "")),
            str(tokens),
        )
    console.print(Panel(table, title=f"[bold cyan]Context[/bold cyan] ({len(rows)} messages, ~{total} tokens)",
                        subtitle="[dim]tokens 为估算值；精确值见状态行（API 上报）[/dim]",
                        border_style="cyan", box=box.ROUNDED))


def print_memory_entries(memories: list[object]) -> None:
    table = Table(box=box.ROUNDED, header_style="bold cyan", border_style="cyan")
    table.add_column("Type", style="bold #f6c177", no_wrap=True)
    table.add_column("Name", style="white")
    table.add_column("Description", style="dim")
    for m in memories:
        table.add_row(
            _safe_text(getattr(m, "type", "")),
            _safe_text(getattr(m, "name", "")),
            _safe_text(getattr(m, "description", "")),
        )
    console.print(Panel(table, title="[bold cyan]Memories[/bold cyan]", border_style="cyan", box=box.ROUNDED))


def _shorten_path(path: str, max_len: int = 44) -> str:
    """把绝对路径缩写：HOME 前缀换 ~；过长时优先保留末尾的路径分量
    （项目名最重要），从前往后逐段丢弃，前缀加 …。"""
    home = str(Path.home())
    if path.startswith(home):
        path = "~" + path[len(home):]
    if len(path) <= max_len:
        return path
    # 从末尾逐段保留，直到放不下为止
    parts = path.split("/")
    kept: list[str] = []
    for seg in reversed(parts):
        candidate = ("/".join([seg] + kept))
        if len(candidate) + 1 > max_len and kept:
            break
        kept.insert(0, seg)
    return "…/" + "/".join(kept)


def _relative_time(iso_time: str) -> str:
    """ISO8601(UTC) 时间转相对显示：just now / 5m ago / 3h ago / 2d ago。"""
    try:
        from datetime import datetime, timezone
        t = datetime.strptime(iso_time, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        delta = (datetime.now(timezone.utc) - t).total_seconds()
    except (ValueError, TypeError):
        return iso_time or "?"
    if delta < 60:
        return "just now"
    if delta < 3600:
        return f"{int(delta // 60)}m ago"
    if delta < 86400:
        return f"{int(delta // 3600)}h ago"
    return f"{int(delta // 86400)}d ago"


def print_session_rows(sessions: list[dict], current_id: str | None = None) -> None:
    """渲染 /sessions 表格：ID / 时间 / 消息数 / 模型 / 工作目录。

    当前会话用 * 标记并高亮；cwd 缩写避免撑爆表格。
    """
    table = Table(box=box.ROUNDED, header_style="bold cyan", border_style="cyan")
    table.add_column("", no_wrap=True)  # 当前会话标记列
    table.add_column("ID", style="bold #f6c177", no_wrap=True)
    table.add_column("When", style="cyan", no_wrap=True)
    table.add_column("Msgs", justify="right", no_wrap=True)
    table.add_column("Model", style="dim", no_wrap=True)
    table.add_column("Working dir", style="white")
    for s in sessions:
        sid = s.get("id", "")
        is_current = sid == current_id
        table.add_row(
            "[bold green]*[/bold green]" if is_current else "",
            ("[bold green]" if is_current else "") + _safe_text(sid) + ("[/bold green]" if is_current else ""),
            _relative_time(s.get("startTime", "")),
            str(s.get("messageCount", 0)),
            _safe_text(s.get("model", "")),
            _safe_text(_shorten_path(s.get("cwd", ""))),
        )
    console.print(Panel(
        table,
        title=f"[bold cyan]Sessions[/bold cyan] ({len(sessions)})",
        subtitle="[dim]/switch <id> 切换 · /sessions rm <id> 删除 · /sessions clean 清理测试残留[/dim]",
        border_style="cyan", box=box.ROUNDED,
    ))


# ─── trace 事件表格 ─────────────────────────────────────────

# 事件类型 → (颜色, 摘要字段优先级)。摘要从事件里挑最有信息量的字段展示。
_TRACE_KIND_STYLE = {
    "turn.start": ("bold cyan", ["user_preview"]),
    "turn.end": ("cyan", ["assistant_preview", "aborted"]),
    "tool.start": ("bold yellow", ["tool", "input"]),
    "tool.end": ("yellow", ["tool", "result_preview", "error"]),
    "bg.start": ("bold magenta", ["job_id", "command"]),
    "bg.done": ("magenta", ["job_id", "exit_code", "command"]),
    "compact": ("bold green", ["trigger", "messages_after"]),
}


def _trace_summary(kind: str, event: dict, max_len: int = 60) -> str:
    """从事件字段里挑最有信息量的内容做单行摘要。"""
    _, fields = _TRACE_KIND_STYLE.get(kind, ("white", []))
    parts = []
    for f in fields:
        if f in event and event[f] not in (None, ""):
            v = event[f]
            if isinstance(v, bool):
                parts.append(f"{f}={v}")
            else:
                parts.append(str(v))
    if not parts:
        rest = {k: v for k, v in event.items() if k not in ("ts", "kind")}
        parts.append(json.dumps(rest, ensure_ascii=False, default=str))
    text = " · ".join(parts).replace("\n", " ")
    return text if len(text) <= max_len else text[:max_len] + "…"


def print_trace_rows(events: list[dict], trace_file: str, enabled: bool) -> None:
    """渲染 /trace 事件表格：时间 / 类型 / 耗时 / 摘要，免翻日志文件。"""
    table = Table(box=box.ROUNDED, header_style="bold cyan", border_style="cyan")
    table.add_column("Time", style="dim", no_wrap=True)
    table.add_column("Event", no_wrap=True)
    table.add_column("Dur", justify="right", no_wrap=True, style="dim")
    table.add_column("Detail", style="white")
    for e in events:
        kind = e.get("kind", "?")
        style, _ = _TRACE_KIND_STYLE.get(kind, ("white", []))
        ts = e.get("ts", "")[11:23]
        dur = e.get("duration_s")
        table.add_row(
            _safe_text(ts),
            f"[{style}]{_safe_text(kind)}[/{style}]",
            f"{dur}s" if dur is not None else "",
            _safe_text(_trace_summary(kind, e)),
        )
    state = "[bold green]ON[/bold green]" if enabled else "[dim]OFF[/dim]"
    console.print(Panel(
        table,
        title=f"[bold cyan]Trace[/bold cyan] ({len(events)} events, logging {state})",
        subtitle=f"[dim]{_safe_text(trace_file)} · /trace on|off 开关 · /trace <n> 看更多[/dim]",
        border_style="cyan", box=box.ROUNDED,
    ))


def print_skill_entries(skills: list[object]) -> None:
    table = Table(box=box.ROUNDED, header_style="bold cyan", border_style="cyan")
    table.add_column("Skill", style="bold #f6c177", no_wrap=True)
    table.add_column("Source", style="cyan", no_wrap=True)
    table.add_column("Mode", style="magenta", no_wrap=True)
    table.add_column("Description", style="white")
    for s in skills:
        name = getattr(s, "name", "")
        tag = f"/{name}" if getattr(s, "user_invocable", False) else name
        table.add_row(
            _safe_text(tag),
            _safe_text(getattr(s, "source", "")),
            _safe_text(getattr(s, "context", "")),
            _safe_text(getattr(s, "description", "")),
        )
    console.print(Panel(table, title="[bold cyan]Skills[/bold cyan]", border_style="cyan", box=box.ROUNDED))


# ─── Tool icons and summaries ───────────────────────────────

_TOOL_ICONS = {
    "read_file": "📖",
    "write_file": "✏️",
    "edit_file": "🔧",
    "list_files": "📁",
    "grep_search": "🔍",
    "run_shell": "💻",
    "skill": "⚡",
    "skill_create": "🍪",
    "skill_evolve": "🧬",
    "agent": "🤖",
}


def _get_tool_icon(name: str) -> str:
    return _TOOL_ICONS.get(name, "🔨")


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
        return f'"{inp.get("pattern", "")}" in {inp.get("path", ".")}'
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
        return f'[{inp.get("type", "general")}] {inp.get("description", "")}'
    return ""
