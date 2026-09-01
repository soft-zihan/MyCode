"""Textual TUI for BearCode - 全面对接后端。

架构对齐 opencode：
- 组件化（Messages, Input, Dialogs）
- 状态管理（reactive state）
- 命令系统（复用 CLI Registry）
- 对话框系统（SelectDialog, ConfirmDialog）
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Optional, Any

from textual.app import App, ComposeResult
from textual.containers import Container, Vertical
from textual.widgets import (
    Header, Footer, Static, Input, ListView, ListItem,
    Label, Button, Markdown, TextArea
)
from textual.reactive import reactive
from textual.binding import Binding
from textual import events
from textual.screen import ModalScreen
from rich.text import Text
from rich.panel import Panel
from rich.markdown import Markdown as RichMarkdown

from .theme import theme


# ============================================================
# State Management - 状态管理
# ============================================================

class AppState:
    """Application state - 对齐 opencode 的状态管理。"""
    
    def __init__(self):
        self.messages: list[dict] = []
        self.is_processing: bool = False
        self.current_session_id: Optional[str] = None
        self.thinking_visible: bool = False
    
    def add_message(self, role: str, content: str, **metadata):
        self.messages.append({
            "role": role,
            "content": content,
            **metadata
        })
    
    def clear_messages(self):
        self.messages.clear()


# ============================================================
# Dialogs - 对话框系统（对齐 opencode）
# ============================================================

class SelectDialog(ModalScreen[str | None]):
    """Select dialog - 对齐 opencode 的 DialogSelect。"""
    
    DEFAULT_CSS = """
    SelectDialog {
        align: center middle;
    }
    
    #dialog {
        width: 80%;
        height: 80%;
        border: thick $primary;
        background: $surface;
        padding: 1;
    }
    
    #title {
        color: $primary;
        text-style: bold;
        margin-bottom: 1;
    }
    
    #filter {
        width: 100%;
        margin-bottom: 1;
    }
    
    #list {
        height: 1fr;
    }
    
    ListItem {
        padding: 0 1;
    }
    
    ListItem:hover {
        background: $primary 30%;
    }
    
    ListItem.--highlight {
        background: $primary;
        color: $text;
    }
    """
    
    def __init__(
        self,
        title: str,
        options: list[tuple[str, str]],
        current: Optional[str] = None,
    ):
        super().__init__()
        self.title_text = title
        self.options = options
        self.current = current
        self.filtered_options = options.copy()
    
    def compose(self) -> ComposeResult:
        with Container(id="dialog"):
            yield Static(self.title_text, id="title")
            yield Input(placeholder="Filter...", id="filter")
            yield ListView(id="list")
    
    def on_mount(self) -> None:
        self._render_list()
        self.query_one("#filter", Input).focus()
    
    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "filter":
            query = event.value.lower()
            self.filtered_options = [
                opt for opt in self.options
                if query in opt[0].lower()
            ]
            self._render_list()
    
    def on_list_view_selected(self, event: ListView.Selected) -> None:
        if event.list_view.id == "list":
            idx = event.list_view.index
            if 0 <= idx < len(self.filtered_options):
                self.dismiss(self.filtered_options[idx][1])
    
    def _render_list(self) -> None:
        list_view = self.query_one("#list", ListView)
        list_view.clear()
        for title, value in self.filtered_options:
            item = ListItem(Label(title))
            if value == self.current:
                item.add_class("--highlight")
            list_view.append(item)


class ConfirmDialog(ModalScreen[bool]):
    """Confirm dialog - 对齐 opencode 的 DialogConfirm。"""
    
    DEFAULT_CSS = """
    ConfirmDialog {
        align: center middle;
    }
    
    #dialog {
        width: 60;
        height: auto;
        border: thick $warning;
        background: $surface;
        padding: 1 2;
    }
    
    #message {
        margin-bottom: 1;
    }
    
    #buttons {
        height: 3;
        layout: horizontal;
    }
    
    Button {
        margin: 0 1;
    }
    
    #yes {
        background: $success;
    }
    
    #no {
        background: $error;
    }
    """
    
    def __init__(self, message: str):
        super().__init__()
        self.message = message
    
    def compose(self) -> ComposeResult:
        with Container(id="dialog"):
            yield Static(self.message, id="message")
            with Container(id="buttons"):
                yield Button("Yes", id="yes")
                yield Button("No", id="no")
    
    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")


# ============================================================
# Messages Component - 消息组件
# ============================================================

class MessagesContainer(Vertical):
    """Messages container - 显示对话历史。"""
    
    DEFAULT_CSS = """
    MessagesContainer {
        height: 1fr;
        padding: 1;
        overflow-y: auto;
    }
    
    .user-message {
        border-left: thick $primary;
        background: $surface;
        padding: 0 1;
        margin: 1 0;
    }
    
    .assistant-message {
        padding: 0 1;
        margin: 1 0;
    }
    
    .system-message {
        color: $text-muted;
        padding: 0 1;
        margin: 1 0;
    }
    """
    
    def __init__(self, state: AppState, **kwargs):
        super().__init__(**kwargs)
        self.state = state
    
    def render_message(self, msg: dict) -> Static:
        role = msg["role"]
        content = msg["content"]
        
        if role == "user":
            return Static(Panel(content, title="You", border_style="blue"), classes="user-message")
        elif role == "assistant":
            return Static(Panel(RichMarkdown(content), title="Assistant", border_style="green"), classes="assistant-message")
        else:
            return Static(content, classes="system-message")
    
    def refresh_messages(self) -> None:
        self.remove_children()
        for msg in self.state.messages:
            self.mount(self.render_message(msg))
        self.scroll_end(animate=False)


# ============================================================
# Input Component - 输入组件
# ============================================================

class InputContainer(Container):
    """Input container - 用户输入。"""
    
    DEFAULT_CSS = """
    InputContainer {
        dock: bottom;
        height: 5;
        padding: 1 2;
        background: $primary-background;
        border-top: solid $primary;
    }
    
    Input {
        width: 100%;
        height: 3;
        background: $surface;
        border: tall $primary;
    }
    
    Input:focus {
        border: tall $accent;
        background: $surface-darken-1;
    }
    """
    
    def compose(self) -> ComposeResult:
        yield Input(placeholder="Type a message or /command...", id="main-input")


# ============================================================
# Main App - 主应用
# ============================================================

class BearCodeApp(App):
    """BearCode TUI - 全面对接后端。"""
    
    CSS = """
    Screen {
        layout: vertical;
    }
    
    #messages {
        height: 1fr;
        padding: 1;
        overflow-y: auto;
    }
    
    #input-container {
        dock: bottom;
        height: 5;
        padding: 1 2;
        background: $primary-background;
        border-top: solid $primary;
    }
    
    #main-input {
        width: 100%;
        height: 3;
        background: $surface;
        border: tall $primary;
    }
    
    #main-input:focus {
        border: tall $accent;
        background: $surface-darken-1;
    }
    """
    
    BINDINGS = [
        Binding("ctrl+k", "command_palette", "Command Palette"),
        Binding("ctrl+l", "clear", "Clear"),
        Binding("ctrl+c", "quit", "Quit"),
        Binding("ctrl+t", "toggle_thinking", "Toggle Thinking"),
        Binding("ctrl+s", "sessions", "Sessions"),
        Binding("ctrl+m", "models", "Models"),
    ]
    
    def __init__(self, agent=None, **kwargs):
        super().__init__(**kwargs)
        self.agent = agent
        self.state = AppState()
        if agent:
            self.state.current_session_id = agent.session_id
    
    def compose(self) -> ComposeResult:
        yield Header()
        yield MessagesContainer(self.state, id="messages")
        yield InputContainer(id="input-container")
        yield Footer()
    
    async def on_mount(self) -> None:
        # Welcome message
        self.state.add_message("system", "Welcome to BearCode!")
        
        if self.agent:
            status = self.agent.status_line()
            self.state.add_message("system", f"Model: {self.agent.model} | {status}")
        
        # Refresh messages
        messages = self.query_one("#messages", MessagesContainer)
        messages.refresh_messages()
        
        # Focus input
        self.query_one("#main-input", Input).focus()
    
    async def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "main-input":
            return
        
        text = event.value.strip()
        if not text:
            return
        
        # Clear input
        event.input.value = ""
        
        # Handle commands
        if text.startswith("/"):
            await self._handle_command(text)
            return
        
        # Handle normal chat - run in background task to avoid blocking UI
        asyncio.create_task(self._handle_chat(text))
    
    async def _handle_command(self, text: str) -> None:
        """Handle slash commands - 直接调用底层 API，不调用 _dispatch_command。"""
        if not self.agent:
            self.state.add_message("system", "[red]No agent configured[/red]")
            self._refresh_messages()
            return
        
        # Parse command
        parts = text[1:].split(maxsplit=1)
        cmd_name = parts[0] if parts else ""
        cmd_args = parts[1] if len(parts) > 1 else ""
        
        try:
            # /help
            if cmd_name in ("help", "h"):
                from agents.cli.registry import registry
                help_text = registry.format_help()
                self.state.add_message("system", f"[cyan]{help_text}[/cyan]")
            
            # /sessions
            elif cmd_name == "sessions":
                from agents.core.session import list_sessions
                sessions = list_sessions()
                if not sessions:
                    self.state.add_message("system", "[yellow]No saved sessions.[/yellow]")
                else:
                    lines = ["[cyan]Sessions:[/cyan]"]
                    for s in sessions[:20]:
                        marker = " (current)" if s["id"] == self.agent.session_id else ""
                        lines.append(f"  {s['id'][:8]}... - {s.get('name', 'Unnamed')}{marker}")
                    self.state.add_message("system", "\n".join(lines))
            
            # /switch
            elif cmd_name == "switch":
                if not cmd_args:
                    self.state.add_message("system", "[red]Usage: /switch <session_id>[/red]")
                else:
                    from agents.core.session import load_session
                    session = load_session(cmd_args)
                    if session:
                        self.agent.restore_session({
                            "openaiMessages": session.get("openaiMessages"),
                            "foldedSessionMemories": session.get("foldedSessionMemories"),
                            "checkpointStore": session.get("checkpointStore"),
                            "turnBoundaries": session.get("turnBoundaries"),
                            "contextStore": session.get("contextStore"),
                        })
                        self.agent.session_id = cmd_args
                        self.state.current_session_id = cmd_args
                        self.state.add_message("system", f"[green]Switched to session {cmd_args[:8]}...[/green]")
                    else:
                        self.state.add_message("system", f"[red]Session not found: {cmd_args}[/red]")
            
            # /models
            elif cmd_name == "models":
                if cmd_args.strip():
                    self.agent.model = cmd_args.strip()
                    self.state.add_message("system", f"[green]Switched to model: {cmd_args.strip()}[/green]")
                else:
                    from agents.model.model_registry import discover_endpoints
                    endpoints = discover_endpoints()
                    lines = [f"[cyan]Current model: {self.agent.model}[/cyan]"]
                    if endpoints:
                        lines.append("")
                        lines.append("[cyan]Configured endpoints:[/cyan]")
                        for eid, ep in sorted(endpoints.items()):
                            model_name = ep.model or "(default)"
                            lines.append(f"  {eid}: {model_name}")
                    else:
                        lines.append("[yellow]No additional endpoints configured.[/yellow]")
                        lines.append("[dim]Set BEAR_ENDPOINT_<ID>_BASE_URL/API_KEY/MODEL to add more.[/dim]")
                    self.state.add_message("system", "\n".join(lines))
            
            # /compact
            elif cmd_name == "compact":
                await self.agent.compact()
                self.state.add_message("system", "[green]Session compacted.[/green]")
            
            # /cost
            elif cmd_name == "cost":
                cost = self.agent._get_current_cost_usd()
                self.state.add_message("system", f"[cyan]Current cost: ${cost:.4f}[/cyan]")
            
            # /status
            elif cmd_name == "status":
                status = self.agent.status_line()
                self.state.add_message("system", f"[cyan]{status}[/cyan]")
            
            # /new
            elif cmd_name == "new":
                self.agent.start_new_session()
                self.state.clear_messages()
                self.state.add_message("system", "[green]New session started.[/green]")
            
            # /clear
            elif cmd_name == "clear":
                self.state.clear_messages()
                self.state.add_message("system", "[green]Messages cleared.[/green]")
            
            # /quit
            elif cmd_name in ("quit", "q", "exit"):
                self.exit()
            
            # /rewind
            elif cmd_name == "rewind":
                n = 1
                if cmd_args.strip():
                    try:
                        n = int(cmd_args.strip())
                    except ValueError:
                        self.state.add_message("system", "[red]Usage: /rewind [N]  (N = number of turns to rewind)[/red]")
                        self._refresh_messages()
                        return
                result = self.agent.rewind(n)
                self.state.add_message("system", f"[green]{result}[/green]")
            
            # /fork
            elif cmd_name == "fork":
                result = self.agent.fork_session()
                self.state.add_message("system", f"[green]{result}[/green]")
            
            # /undo
            elif cmd_name == "undo":
                result = self.agent.rewind(1)
                self.state.add_message("system", f"[green]{result}[/green]")
            
            # /rename
            elif cmd_name == "rename":
                name = cmd_args.strip()
                if not name:
                    self.state.add_message("system", "[red]Usage: /rename <name>[/red]")
                else:
                    from agents.core.session import save_session_meta
                    if save_session_meta(self.agent.session_id, {"title": name}):
                        self.state.add_message("system", f"[green]Session renamed to: {name}[/green]")
                    else:
                        self.state.add_message("system", "[red]Failed to rename session[/red]")
            
            # /resume
            elif cmd_name == "resume":
                if not cmd_args:
                    from agents.core.session import list_sessions
                    sessions = list_sessions()
                    if not sessions:
                        self.state.add_message("system", "[yellow]No saved sessions to resume.[/yellow]")
                    else:
                        lines = ["[cyan]Recent sessions:[/cyan]"]
                        for i, s in enumerate(sessions[:10], 1):
                            lines.append(f"  {i}. {s['id'][:8]}... - {s.get('name', 'Unnamed')}")
                        lines.append("[cyan]Usage: /resume <session_id>[/cyan]")
                        self.state.add_message("system", "\n".join(lines))
                else:
                    from agents.core.session import load_session
                    session = load_session(cmd_args)
                    if session:
                        self.agent.restore_session({
                            "openaiMessages": session.get("openaiMessages"),
                            "foldedSessionMemories": session.get("foldedSessionMemories"),
                            "checkpointStore": session.get("checkpointStore"),
                            "turnBoundaries": session.get("turnBoundaries"),
                            "contextStore": session.get("contextStore"),
                        })
                        self.agent.session_id = cmd_args
                        self.state.current_session_id = cmd_args
                        self.state.add_message("system", f"[green]Resumed session {cmd_args[:8]}...[/green]")
                    else:
                        self.state.add_message("system", f"[red]Session not found: {cmd_args}[/red]")
            
            # /subagents
            elif cmd_name in ("subagents", "sa", "tasks"):
                from agents.tui.subagent_tracker import get_tracker
                tracker = get_tracker()
                tasks = tracker.get_recent_tasks(20)
                if not tasks:
                    self.state.add_message("system", "[yellow]No recent subagent tasks.[/yellow]")
                else:
                    lines = ["[cyan]Recent subagent tasks:[/cyan]"]
                    for task in tasks:
                        status = task.get("status", "unknown")
                        desc = task.get("description", "No description")
                        lines.append(f"  [{status}] {desc}")
                    self.state.add_message("system", "\n".join(lines))
            
            # /thinking
            elif cmd_name == "thinking":
                from agents.config import thinking_visible, set_thinking_visible
                new_state = not thinking_visible()
                set_thinking_visible(new_state)
                self.state.thinking_visible = new_state
                self.state.add_message("system", f"[green]Thinking display: {'ON' if new_state else 'OFF'}[/green]")
            
            # /plan
            elif cmd_name == "plan":
                self.agent.toggle_plan_mode()
                self.state.add_message("system", "[green]Plan mode toggled.[/green]")
            
            # /permission
            elif cmd_name == "permission":
                mode = cmd_args.strip().lower()
                valid_modes = ["default", "acceptEdits", "bypassPermissions", "plan", "dontAsk"]
                if not mode:
                    self.state.add_message("system", f"[cyan]Current permission mode: {self.agent.permission_mode}\n\nValid modes: {', '.join(valid_modes)}\n\nUsage: /permission <mode>\n\nQuick: /yolo on|off[/cyan]")
                elif mode in valid_modes:
                    from agents.core.permission_set import get_permission_set_from_legacy_mode
                    self.agent.permission_mode = mode
                    self.agent._permission_set = get_permission_set_from_legacy_mode(mode)
                    self.agent._tool_executor.permission_set = self.agent._permission_set
                    self.state.add_message("system", f"[green]Permission mode set to: {mode}[/green]")
                else:
                    self.state.add_message("system", f"[red]Invalid mode: {mode}. Valid modes: {', '.join(valid_modes)}[/red]")
            
            # /yolo
            elif cmd_name == "yolo":
                arg = cmd_args.strip().lower()
                if arg == "on":
                    from agents.core.permission_set import get_permission_set_from_legacy_mode
                    self.agent.permission_mode = "bypassPermissions"
                    self.agent._permission_set = get_permission_set_from_legacy_mode("bypassPermissions")
                    self.agent._tool_executor.permission_set = self.agent._permission_set
                    self.state.add_message("system", "[yellow]YOLO mode: ON - all permissions bypassed[/yellow]")
                elif arg == "off":
                    from agents.core.permission_set import get_permission_set_from_legacy_mode
                    self.agent.permission_mode = "default"
                    self.agent._permission_set = get_permission_set_from_legacy_mode("default")
                    self.agent._tool_executor.permission_set = self.agent._permission_set
                    self.state.add_message("system", "[green]YOLO mode: OFF - default permissions[/green]")
                else:
                    self.state.add_message("system", "[red]Usage: /yolo on|off[/red]")
            
            # /cd
            elif cmd_name == "cd":
                import os
                target = cmd_args.strip()
                if not target:
                    self.state.add_message("system", f"[cyan]Current directory: {Path.cwd()}[/cyan]")
                else:
                    new_dir = Path(os.path.expanduser(target))
                    if not new_dir.is_absolute():
                        new_dir = Path.cwd() / new_dir
                    try:
                        new_dir = new_dir.resolve()
                        if not new_dir.is_dir():
                            self.state.add_message("system", f"[red]Not a directory: {new_dir}[/red]")
                        else:
                            os.chdir(new_dir)
                            self.state.add_message("system", f"[green]Changed to: {new_dir}[/green]")
                    except Exception as e:
                        self.state.add_message("system", f"[red]Error: {e}[/red]")
            
            # /context
            elif cmd_name == "context":
                rows = self.agent.describe_context()
                if not rows:
                    self.state.add_message("system", "[yellow]Context is empty.[/yellow]")
                else:
                    lines = ["[cyan]Context:[/cyan]"]
                    for row in rows:
                        if isinstance(row, dict):
                            idx = row.get("index", "?")
                            role = row.get("role", "?")
                            preview = row.get("preview", "")
                            lines.append(f"  [{idx}] {role}: {preview}")
                        else:
                            lines.append(f"  {row}")
                    self.state.add_message("system", "\n".join(lines))
            
            # /memory
            elif cmd_name == "memory":
                if cmd_args.startswith("prune"):
                    from agents.memory.memory import auto_prune_memories
                    prune_args = cmd_args[5:].strip().split()
                    dry_run = "--dry-run" in prune_args
                    threshold = 0.1
                    for arg in prune_args:
                        if arg.startswith("--threshold="):
                            try:
                                threshold = float(arg.split("=")[1])
                            except ValueError:
                                pass
                    result = auto_prune_memories(dry_run=dry_run, threshold=threshold)
                    self.state.add_message("system", f"[green]{result}[/green]")
                else:
                    self.state.add_message("system", "[red]Usage: /memory prune [--dry-run] [--threshold=0.1][[/red]")
            
            # /skills
            elif cmd_name == "skills":
                from agents.skills.skills import discover_skills
                skills = discover_skills()
                if not skills:
                    self.state.add_message("system", "[yellow]No skills found. Add skills to .bear/skills/<name>/SKILL.md[/yellow]")
                else:
                    lines = ["[cyan]Available skills:[/cyan]"]
                    for skill in skills:
                        name = skill.name if hasattr(skill, 'name') else str(skill)
                        desc = skill.description if hasattr(skill, 'description') else "No description"
                        lines.append(f"  {name}: {desc}")
                    self.state.add_message("system", "\n".join(lines))
            
            # Unknown command
            else:
                self.state.add_message("system", f"[red]Unknown command: /{cmd_name}. Type /help for available commands.[/red]")
        
        except Exception as e:
            self.state.add_message("system", f"[red]Error: {e}[/red]")
        
        self._refresh_messages()
    
    async def _handle_chat(self, text: str) -> None:
        """Handle normal chat - 使用 chat_stream 对接后端。"""
        if not self.agent:
            self.state.add_message("system", "[red]No agent configured[/red]")
            self._refresh_messages()
            return
        
        # Add user message
        self.state.add_message("user", text)
        self._refresh_messages()
        
        # Process with agent using stream
        self.state.is_processing = True
        response_parts = []
        
        try:
            # Set up TUI callbacks for confirm and plan approval
            async def tui_confirm(message: str) -> bool:
                # Show confirm dialog and wait for result
                result = await self.push_screen_wait(ConfirmDialog(message))
                return result
            
            async def tui_plan_approval(plan: str) -> bool:
                # Show plan as system message and ask for confirmation
                self.state.add_message("system", f"[cyan]Plan: {plan}[/cyan]")
                self._refresh_messages()
                result = await self.push_screen_wait(ConfirmDialog("Approve this plan?"))
                return result
            
            self.agent.set_confirm_fn(tui_confirm)
            self.agent.set_plan_approval_fn(tui_plan_approval)
            
            # Use chat_stream to get events
            async for event in self.agent.chat_stream(text):
                event_type = event.get("type")
                
                if event_type == "text":
                    content = event.get("content", "")
                    response_parts.append(content)
                
                elif event_type == "tool_call":
                    tool_name = event.get("name", "unknown")
                    tool_input = event.get("input", {})
                    # Format tool call nicely
                    input_summary = str(tool_input)[:100]
                    self.state.add_message("system", f"[yellow]⚡ Tool call: {tool_name}[/yellow]\n[dim]{input_summary}...[/dim]")
                    self._refresh_messages()
                
                elif event_type == "tool_result":
                    result = event.get("result", "")
                    # Format tool result nicely
                    result_summary = str(result)[:200]
                    self.state.add_message("system", f"[green]✓ Tool result[/green]\n[dim]{result_summary}...[/dim]")
                    self._refresh_messages()
                
                elif event_type == "thinking":
                    thinking_text = event.get("content", "")
                    if self.state.thinking_visible:
                        self.state.add_message("system", f"[dim italic]💭 Thinking: {thinking_text}[/dim italic]")
                        self._refresh_messages()
                
                elif event_type == "sub_agent_start":
                    agent_type = event.get("agent_type", "unknown")
                    description = event.get("description", "")
                    self.state.add_message("system", f"[blue]🤖 Starting sub-agent: {agent_type}[/blue]\n[dim]{description}[/dim]")
                    self._refresh_messages()
                
                elif event_type == "sub_agent_end":
                    agent_type = event.get("agent_type", "unknown")
                    self.state.add_message("system", f"[blue]✓ Sub-agent {agent_type} completed[/blue]")
                    self._refresh_messages()
                
                elif event_type == "done":
                    break
                
                elif event_type == "error":
                    error_msg = event.get("message", "Unknown error")
                    self.state.add_message("system", f"[red]Error: {error_msg}[/red]")
                    self._refresh_messages()
                    break
            
            # Add response if we got text
            if response_parts:
                full_response = "".join(response_parts)
                self.state.add_message("assistant", full_response)
            
        except Exception as e:
            self.state.add_message("system", f"[red]Error: {e}[/red]")
        finally:
            self.state.is_processing = False
            self._refresh_messages()
    
    def _refresh_messages(self) -> None:
        messages = self.query_one("#messages", MessagesContainer)
        messages.refresh_messages()
    
    # Actions
    def action_command_palette(self) -> None:
        """Open command palette."""
        from agents.cli.registry import registry
        commands = registry.list_all()
        options = [(f"/{cmd.name} - {cmd.description}", f"/{cmd.name}") for cmd in commands]
        
        self.push_screen(
            SelectDialog("Command Palette", options),
            callback=self._on_command_selected
        )
    
    def _on_command_selected(self, command: str | None) -> None:
        if command:
            asyncio.create_task(self._handle_command(command))
    
    def action_clear(self) -> None:
        """Clear messages."""
        self.state.clear_messages()
        self._refresh_messages()
    
    def action_sessions(self) -> None:
        """Open sessions dialog."""
        if not self.agent:
            return
        
        from agents.core.session import list_sessions
        sessions = list_sessions()
        options = [
            (f"{s['id'][:8]} - {s.get('name', 'Unnamed')}", s['id'])
            for s in sessions[:20]
        ]
        
        self.push_screen(
            SelectDialog("Sessions", options, self.state.current_session_id),
            callback=self._on_session_selected
        )
    
    def _on_session_selected(self, session_id: str | None) -> None:
        if session_id and self.agent:
            # TODO: Implement session switch
            self.notify(f"Switch to session: {session_id}")
    
    def action_models(self) -> None:
        """Open models dialog."""
        if not self.agent:
            return
        
        from agents.model.model_registry import discover_endpoints
        endpoints = discover_endpoints()
        options = [(f"{eid}: {ep.model or '(default)'}", eid) for eid, ep in sorted(endpoints.items())]
        
        self.push_screen(
            SelectDialog("Models", options, self.agent.model),
            callback=self._on_model_selected
        )
    
    def _on_model_selected(self, endpoint_id: str | None) -> None:
        if endpoint_id and self.agent:
            from agents.model.model_registry import discover_endpoints
            endpoints = discover_endpoints()
            if endpoint_id in endpoints:
                ep = endpoints[endpoint_id]
                self.agent.model = ep.model or self.agent.model
                if ep.base_url:
                    self.agent.api_base = ep.base_url
                if ep.api_key:
                    self.agent.api_key = ep.api_key
                self.notify(f"Switched to endpoint: {endpoint_id}")


# ============================================================
# Entry Points
# ============================================================

def run_app() -> None:
    """Run the BearCode TUI application."""
    app = BearCodeApp()
    app.run()


def run_textual_app(args) -> None:
    """Run the Textual TUI application with agent configuration."""
    from ..agent import Agent
    from ..session import load_session, get_latest_session_id
    from ..main import _resolve_permission_mode, _resolve_model, _resolve_api_config, _load_env_file
    
    _load_env_file()
    
    permission_mode = _resolve_permission_mode(args)
    model = _resolve_model(args.model)
    resolved_api_base, resolved_api_key = _resolve_api_config(args.api_base)
    
    if not resolved_api_key:
        from ..ui import print_error
        print_error("API key is required. Set APIKEY in .env or use ANTHROPIC_API_KEY.")
        return
    
    agent = Agent(
        permission_mode=permission_mode,
        model=model,
        thinking=args.thinking,
        max_cost_usd=args.max_cost,
        max_turns=args.max_turns,
        api_base=resolved_api_base,
        api_key=resolved_api_key,
    )
    
    # Resume session if requested
    session_to_resume = None
    if args.session:
        session_to_resume = args.session
    elif args.resume:
        session_to_resume = get_latest_session_id()
    
    if session_to_resume:
        session = load_session(session_to_resume)
        if session:
            if args.fork:
                agent.restore_session({
                    "openaiMessages": session.get("openaiMessages"),
                    "foldedSessionMemories": session.get("foldedSessionMemories"),
                    "checkpointStore": session.get("checkpointStore"),
                    "turnBoundaries": session.get("turnBoundaries"),
                    "contextStore": session.get("contextStore"),
                })
                agent.fork_session()
            else:
                agent.session_id = session_to_resume
                agent.restore_session({
                    "openaiMessages": session.get("openaiMessages"),
                    "foldedSessionMemories": session.get("foldedSessionMemories"),
                    "checkpointStore": session.get("checkpointStore"),
                    "turnBoundaries": session.get("turnBoundaries"),
                    "contextStore": session.get("contextStore"),
                })
    
    app = BearCodeApp(agent=agent)
    app.run()
