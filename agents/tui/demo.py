#!/usr/bin/env python3
"""Demo script to showcase Textual TUI features.

Run: python -m agents.tui.demo
"""

from textual.app import App, ComposeResult
from textual.containers import Container, Vertical, Horizontal
from textual.widgets import Header, Footer, Static, RichLog, Input, Button
from textual.binding import Binding
from textual.screen import ModalScreen
from rich.panel import Panel
from rich.text import Text

from .textual_app import (
    ThinkingBlock, ToolCallBlock, MessageBlock, 
    SubagentProgress, PermissionDialog, DiffViewer
)


class DemoApp(App):
    """Demo app showcasing Textual TUI features."""
    
    CSS = """
    Screen {
        layout: vertical;
    }
    
    #demo-area {
        height: 1fr;
        padding: 1;
    }
    
    #input-container {
        dock: bottom;
        height: 3;
        padding: 1;
        background: $surface;
    }
    
    #button-bar {
        dock: bottom;
        height: 3;
        padding: 0 1;
        background: $surface;
    }
    
    Button {
        margin: 0 1;
    }
    """
    
    BINDINGS = [
        Binding("ctrl+c", "quit", "Quit"),
        Binding("p", "permission_dialog", "Permission Dialog"),
    ]
    
    def compose(self) -> ComposeResult:
        yield Header()
        
        log = RichLog(id="demo-area", highlight=True, markup=True)
        yield log
        
        with Horizontal(id="button-bar"):
            yield Button("1. Thinking", id="btn1", variant="primary")
            yield Button("2. Tool Call", id="btn2", variant="success")
            yield Button("3. Message", id="btn3", variant="warning")
            yield Button("4. Subagent", id="btn4", variant="error")
            yield Button("5. Permission", id="btn5", variant="default")
        
        yield Footer()
    
    def on_mount(self) -> None:
        """Add demo elements."""
        log = self.query_one("#demo-area", RichLog)
        
        # Welcome message
        log.write(Panel(
            "[bold]BearCode Textual TUI Demo[/bold]\n\n"
            "Features:\n"
            "1. Clickable thinking blocks (click to expand/collapse)\n"
            "2. Tool call display with icons\n"
            "3. Message blocks with borders\n"
            "4. Subagent progress (click to expand)\n"
            "5. Permission dialogs\n\n"
            "Try clicking on the elements below!",
            title="Welcome",
            border_style="primary"
        ))
        
        # Add demo thinking block
        log.write("\n[bold cyan]Demo: Thinking Block (click to expand)[/bold cyan]")
        thinking = ThinkingBlock(
            content="Let me analyze the code structure...\n"
                    "I see there are 3 main components:\n"
                    "- Agent class\n"
                    "- Tool executor\n"
                    "- Session manager\n\n"
                    "The Agent class handles the main conversation loop,\n"
                    "while ToolExecutor manages tool permissions and execution.",
            summary="Analyzing code structure",
            duration="2.1s"
        )
        log.write(thinking)
        
        # Add demo tool calls
        log.write("\n[bold cyan]Demo: Tool Calls[/bold cyan]")
        
        tool1 = ToolCallBlock(
            tool_name="grep_search",
            tool_input={"pattern": "def main", "path": "src/"},
            result="src/main.py:42: def main():\nsrc/utils.py:10: def main_helper():",
            status="success"
        )
        log.write(tool1)
        
        tool2 = ToolCallBlock(
            tool_name="read_file",
            tool_input={"file_path": "src/config.py"},
            result="# Configuration module\nimport os\n\nDEBUG = True",
            status="success"
        )
        log.write(tool2)
        
        tool3 = ToolCallBlock(
            tool_name="run_shell",
            tool_input={"command": "npm test"},
            result="Error: Command failed with exit code 1",
            status="error"
        )
        log.write(tool3)
        
        # Add demo messages
        log.write("\n[bold cyan]Demo: Messages (click to interact)[/bold cyan]")
        
        user_msg = MessageBlock(
            role="user",
            content="Fix the bug in main.py",
            message_index=1
        )
        log.write(user_msg)
        
        assistant_msg = MessageBlock(
            role="assistant",
            content="I found the bug. The issue is in line 42 where the function doesn't handle null values properly. I'll add a null check before processing.",
            footer="▣ claude-sonnet-4 · 3.2s",
            message_index=2
        )
        log.write(assistant_msg)
        
        # Add demo subagent progress
        log.write("\n[bold cyan]Demo: Subagent Progress (click to expand)[/bold cyan]")
        subagents = SubagentProgress(
            tasks=[
                {"type": "explore", "description": "Find API endpoints", "status": "completed", "duration": "1.2s"},
                {"type": "general", "description": "Fix authentication bug", "status": "running", "duration": "3.5s"},
                {"type": "plan", "description": "Design refactoring plan", "status": "completed", "duration": "2.8s"},
            ]
        )
        log.write(subagents)
        
        # Add demo diff viewer
        log.write("\n[bold cyan]Demo: Diff Viewer (for file edits)[/bold cyan]")
        old_code = """def calculate_total(items):
    total = 0
    for item in items:
        total += item.price
    return total"""
        
        new_code = """def calculate_total(items, tax_rate=0.0):
    total = 0
    for item in items:
        total += item.price
    # Add tax calculation
    total_with_tax = total * (1 + tax_rate)
    return total_with_tax"""
        
        diff_viewer = DiffViewer("calculator.py", old_code, new_code)
        log.write(diff_viewer)
        
        log.write("\n[dim]Click on elements to interact • Press 'p' for permission dialog[/dim]")
    
    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Handle button press."""
        log = self.query_one("#demo-area", RichLog)
        btn_id = event.button.id
        
        if btn_id == "btn1":
            log.write(ThinkingBlock(
                content="New thinking block added!\nThis demonstrates the collapsible feature.",
                summary="User added",
                duration="0.1s"
            ))
        elif btn_id == "btn2":
            log.write(ToolCallBlock(
                tool_name="edit_file",
                tool_input={"file_path": "example.py"},
                result="✓ Updated example.py\n@@ -10,3 +10,4 @@\n-old line\n+new line",
                status="success"
            ))
        elif btn_id == "btn3":
            log.write(MessageBlock(
                role="user",
                content="New message from user",
                message_index=3
            ))
        elif btn_id == "btn4":
            log.write(SubagentProgress(
                tasks=[
                    {"type": "explore", "description": "New task", "status": "running", "duration": "0.0s"},
                ]
            ))
        elif btn_id == "btn5":
            self.action_permission_dialog()
    
    def action_permission_dialog(self) -> None:
        """Show permission dialog with diff."""
        old_code = """def hello():
    print("Hello")"""
        
        new_code = """def hello(name="World"):
    print(f"Hello, {name}!")"""
        
        self.push_screen(
            PermissionDialog(
                tool_name="edit_file",
                tool_input={"file_path": "hello.py"},
                message="Update hello function to accept name parameter",
                old_content=old_code,
                new_content=new_code
            )
        )


def run_demo():
    """Run the demo app."""
    app = DemoApp()
    app.run()


if __name__ == "__main__":
    run_demo()
