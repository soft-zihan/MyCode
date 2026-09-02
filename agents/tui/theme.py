"""Color theme aligned with opencode's design system."""

from __future__ import annotations


class Theme:
    """OpenCode-aligned color theme for MyCode TUI."""

    PRIMARY = "#fab283"
    SECONDARY = "#5c9cf5"
    ACCENT = "#9d7cd8"
    ERROR = "#e06c75"
    WARNING = "#f5a742"
    SUCCESS = "#7fd88f"
    INFO = "#56b6c2"

    TEXT = "#eeeeee"
    TEXT_MUTED = "#808080"
    BACKGROUND = "#0a0a0a"
    BACKGROUND_PANEL = "#141414"
    BACKGROUND_ELEMENT = "#1e1e1e"
    BORDER = "#484848"
    BORDER_ACTIVE = "#606060"

    HEADING = ACCENT
    LINK = PRIMARY
    CODE = SUCCESS
    STRONG = WARNING
    EMPHASIS = "#e5c07b"
    BLOCKQUOTE = EMPHASIS

    THINKING_OPACITY = 0.6


theme = Theme()
