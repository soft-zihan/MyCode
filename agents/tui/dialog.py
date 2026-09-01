"""Interactive dialog components using prompt_toolkit.

Provides opencode-style interactive dialogs with:
- Keyboard navigation (up/down/enter/esc)
- Search/filter functionality
- Action buttons
- Mouse support (where available)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Generic, TypeVar, Any

from prompt_toolkit import Application
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.document import Document
from prompt_toolkit.enums import EditingMode
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import HTML, FormattedText
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Layout
from prompt_toolkit.layout.containers import HSplit, VSplit, Window
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.styles import Style
from prompt_toolkit.widgets import Box, Frame, Label, TextArea

from .theme import theme


T = TypeVar("T")


@dataclass
class DialogOption(Generic[T]):
    """A selectable option in a dialog."""
    title: str
    value: T
    description: str = ""
    footer: str = ""
    category: str = ""
    disabled: bool = False
    # Callback when option is selected
    on_select: Callable[[T], None] | None = None


@dataclass
class DialogAction:
    """An action that can be triggered on the selected option."""
    title: str
    key: str  # Key binding like "d" for delete
    callback: Callable[[Any], None]


@dataclass
class DialogSelectState:
    """State for the select dialog."""
    selected_index: int = 0
    filter_text: str = ""
    scroll_offset: int = 0


class SelectDialog(Generic[T]):
    """Interactive select dialog with search and keyboard navigation.
    
    Similar to opencode's DialogSelect component.
    
    Usage:
        options = [
            DialogOption(title="Session 1", value="id1", footer="2h ago"),
            DialogOption(title="Session 2", value="id2", footer="1d ago"),
        ]
        result = SelectDialog(
            title="Sessions",
            options=options,
            actions=[DialogAction("Delete", "d", delete_fn)],
        ).run()
    """
    
    def __init__(
        self,
        title: str,
        options: list[DialogOption[T]],
        actions: list[DialogAction] | None = None,
        placeholder: str = "Search...",
        current: T | None = None,
    ):
        self.title = title
        self.options = options
        self.actions = actions or []
        self.placeholder = placeholder
        self.current = current
        self.state = DialogSelectState()
        self.result: T | None = None
        self.confirmed = False
        
        # Set initial selection to current
        if current is not None:
            for i, opt in enumerate(options):
                if opt.value == current:
                    self.state.selected_index = i
                    break
    
    @property
    def filtered_options(self) -> list[DialogOption[T]]:
        """Get options filtered by search text."""
        if not self.state.filter_text:
            return [o for o in self.options if not o.disabled]
        
        query = self.state.filter_text.lower()
        return [
            o for o in self.options 
            if not o.disabled and (
                query in o.title.lower() or 
                query in o.description.lower() or
                query in o.footer.lower()
            )
        ]
    
    def _get_display_options(self) -> list[tuple[str, bool, bool]]:
        """Get options with formatting: (text, is_selected, is_current)."""
        filtered = self.filtered_options
        result = []
        for i, opt in enumerate(filtered):
            is_selected = i == self.state.selected_index
            is_current = opt.value == self.current
            result.append((opt, is_selected, is_current))
        return result
    
    def _move_selection(self, direction: int) -> None:
        """Move selection up or down."""
        filtered = self.filtered_options
        if not filtered:
            return
        
        new_index = self.state.selected_index + direction
        if new_index < 0:
            new_index = len(filtered) - 1
        elif new_index >= len(filtered):
            new_index = 0
        
        self.state.selected_index = new_index
    
    def _create_key_bindings(self) -> KeyBindings:
        """Create key bindings for the dialog."""
        kb = KeyBindings()
        
        @kb.add("up")
        @kb.add("c-p")
        def _move_up(event):
            self._move_selection(-1)
            self._refresh()
        
        @kb.add("down")
        @kb.add("c-n")
        def _move_down(event):
            self._move_selection(1)
            self._refresh()
        
        @kb.add("home")
        def _move_home(event):
            self.state.selected_index = 0
            self._refresh()
        
        @kb.add("end")
        def _move_end(event):
            filtered = self.filtered_options
            if filtered:
                self.state.selected_index = len(filtered) - 1
            self._refresh()
        
        @kb.add("enter")
        def _select(event):
            filtered = self.filtered_options
            if filtered and 0 <= self.state.selected_index < len(filtered):
                self.result = filtered[self.state.selected_index].value
                self.confirmed = True
            event.app.exit()
        
        @kb.add("escape")
        def _cancel(event):
            event.app.exit()
        
        @kb.add("c-c")
        def _ctrl_c(event):
            event.app.exit()
        
        # Action key bindings
        for action in self.actions:
            @kb.add(action.key)
            def _trigger_action(event, action=action):
                filtered = self.filtered_options
                if filtered and 0 <= self.state.selected_index < len(filtered):
                    option = filtered[self.state.selected_index]
                    action.callback(option.value)
                    self._refresh()
        
        return kb
    
    def _refresh(self) -> None:
        """Refresh the display."""
        if hasattr(self, "_app") and self._app:
            self._app.invalidate()
    
    def _get_header_text(self) -> FormattedText:
        """Get header text with title and hints."""
        hints = []
        for action in self.actions:
            hints.append(f"[{action.key}] {action.title}")
        
        hint_str = "  ".join(hints) if hints else ""
        
        return FormattedText([
            (theme.PRIMARY, f" {self.title} "),
            ("", "  "),
            (theme.TEXT_MUTED, hint_str),
            ("", "  "),
            (theme.TEXT_MUTED, "esc to close"),
        ])
    
    def _get_footer_text(self) -> FormattedText:
        """Get footer text with navigation hints."""
        return FormattedText([
            (theme.TEXT_MUTED, " ↑↓ navigate  enter select  esc cancel "),
        ])
    
    def _get_options_text(self) -> FormattedText:
        """Get formatted options list."""
        parts = []
        display_options = self._get_display_options()
        
        for opt, is_selected, is_current in display_options:
            # Selection indicator
            if is_current:
                parts.append((theme.ACCENT, " ● "))
            elif is_selected:
                parts.append((theme.PRIMARY, " ▸ "))
            else:
                parts.append(("", "   "))
            
            # Title
            if is_selected:
                parts.append((theme.PRIMARY, opt.title))
            else:
                parts.append((theme.TEXT, opt.title))
            
            # Description
            if opt.description:
                parts.append((theme.TEXT_MUTED, f" {opt.description}"))
            
            # Footer (right-aligned info)
            if opt.footer:
                # Add padding to push footer to right
                parts.append(("", "  "))
                parts.append((theme.TEXT_MUTED, opt.footer))
            
            parts.append(("", "\n"))
        
        if not display_options:
            parts.append((theme.TEXT_MUTED, " No results found"))
            parts.append(("", "\n"))
        
        return FormattedText(parts)
    
    def _build_app(self) -> Application:
        """Build the prompt_toolkit Application for this dialog."""
        filter_textarea = TextArea(
            text="",
            multiline=False,
            focusable=True,
            prompt="> ",
            style=theme.TEXT,
        )
        filter_textarea.buffer.on_text_changed += lambda buf: self._on_filter_changed(buf.text)

        options_window = Window(
            content=FormattedTextControl(self._get_options_text, focusable=False),
            wrap_lines=False,
        )

        header = Window(
            content=FormattedTextControl(self._get_header_text, focusable=False),
        )
        footer = Window(
            content=FormattedTextControl(self._get_footer_text, focusable=False),
        )

        body = HSplit([
            header,
            Box(filter_textarea, padding_left=2, padding_right=2),
            options_window,
            footer,
        ])

        kb = self._create_key_bindings()
        style = Style.from_dict({
            "": theme.TEXT,
            "textarea": theme.TEXT,
            "textarea.prompt": theme.SECONDARY,
        })

        return Application(
            layout=Layout(body, focused_element=filter_textarea),
            key_bindings=kb,
            style=style,
            full_screen=True,
            mouse_support=True,
        )

    def run(self) -> T | None:
        """Run the dialog and return the selected value, or None if cancelled."""
        self._app = self._build_app()
        self._app.run()
        return self.result if self.confirmed else None

    async def run_async(self) -> T | None:
        """Run the dialog asynchronously and return the selected value, or None if cancelled."""
        self._app = self._build_app()
        await self._app.run_async()
        return self.result if self.confirmed else None
    
    def _on_filter_changed(self, text: str) -> None:
        """Handle filter text change."""
        self.state.filter_text = text
        self.state.selected_index = 0
        self._refresh()


async def select_dialog(
    title: str,
    options: list[tuple[str, T]] | list[DialogOption[T]],
    current: T | None = None,
    actions: list[DialogAction] | None = None,
) -> T | None:
    """Show an interactive select dialog.
    
    Args:
        title: Dialog title
        options: List of (title, value) tuples or DialogOption objects
        current: Currently selected value (highlighted)
        actions: List of actions that can be triggered
    
    Returns:
        Selected value, or None if cancelled
    
    Example:
        sessions = [("Session 1", "id1"), ("Session 2", "id2")]
        result = await select_dialog("Sessions", sessions, current="id1")
    """
    # Convert tuples to DialogOption
    dialog_options = []
    for opt in options:
        if isinstance(opt, DialogOption):
            dialog_options.append(opt)
        else:
            title_str, value = opt
            dialog_options.append(DialogOption(title=title_str, value=value))
    
    dialog = SelectDialog(
        title=title,
        options=dialog_options,
        actions=actions,
        current=current,
    )
    return await dialog.run_async()
