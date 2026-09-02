"""Prompt history with frecency ranking.

Tracks user input history and provides frecency-based (frequency + recency)
ranking for autocomplete suggestions.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator


@dataclass
class HistoryEntry:
    """A single history entry with timestamp and usage count."""
    text: str
    timestamp: float = field(default_factory=time.time)
    count: int = 1
    
    @property
    def frecency_score(self) -> float:
        """Calculate frecency score (frequency + recency).
        
        Score = count * recency_weight
        recency_weight decays over time:
        - Last hour: 1.0
        - Last day: 0.8
        - Last week: 0.5
        - Last month: 0.3
        - Older: 0.1
        """
        age_hours = (time.time() - self.timestamp) / 3600
        
        if age_hours < 1:
            recency = 1.0
        elif age_hours < 24:
            recency = 0.8
        elif age_hours < 24 * 7:
            recency = 0.5
        elif age_hours < 24 * 30:
            recency = 0.3
        else:
            recency = 0.1
        
        return self.count * recency


class PromptHistory:
    """Manages prompt history with frecency ranking."""
    
    MAX_ENTRIES = 1000
    HISTORY_FILE = ".MyCode_history.json"
    
    def __init__(self, history_file: Path | None = None):
        self._entries: list[HistoryEntry] = []
        self._history_file = history_file or self._default_history_file()
        self._load()
    
    def _default_history_file(self) -> Path:
        """Get default history file path."""
        # Store in user's home directory
        return Path.home() / self.HISTORY_FILE
    
    def _load(self) -> None:
        """Load history from file."""
        if not self._history_file.exists():
            return
        
        try:
            data = json.loads(self._history_file.read_text())
            for entry_data in data.get("entries", []):
                entry = HistoryEntry(
                    text=entry_data["text"],
                    timestamp=entry_data.get("timestamp", time.time()),
                    count=entry_data.get("count", 1),
                )
                self._entries.append(entry)
        except (json.JSONDecodeError, KeyError, TypeError):
            # Corrupted file, start fresh
            self._entries = []
    
    def _save(self) -> None:
        """Save history to file."""
        try:
            self._history_file.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "entries": [
                    {
                        "text": e.text,
                        "timestamp": e.timestamp,
                        "count": e.count,
                    }
                    for e in self._entries[-self.MAX_ENTRIES:]
                ]
            }
            self._history_file.write_text(json.dumps(data, indent=2))
        except OSError:
            pass  # Ignore save errors
    
    def add(self, text: str) -> None:
        """Add a prompt to history.
        
        If the exact text exists, increment its count and update timestamp.
        Otherwise, add as new entry.
        """
        text = text.strip()
        if not text:
            return
        
        # Check if exists
        for entry in self._entries:
            if entry.text == text:
                entry.count += 1
                entry.timestamp = time.time()
                self._save()
                return
        
        # Add new entry
        self._entries.append(HistoryEntry(text=text))
        
        # Trim if too many
        if len(self._entries) > self.MAX_ENTRIES:
            # Remove oldest/lowest score entries
            self._entries.sort(key=lambda e: e.frecency_score, reverse=True)
            self._entries = self._entries[:self.MAX_ENTRIES]
        
        self._save()
    
    def search(self, prefix: str = "", limit: int = 10) -> list[str]:
        """Search history with frecency ranking.
        
        Args:
            prefix: Optional prefix to filter by
            limit: Maximum results to return
        
        Returns:
            List of matching prompts, sorted by frecency score
        """
        # Filter by prefix if provided
        if prefix:
            prefix_lower = prefix.lower()
            candidates = [e for e in self._entries if e.text.lower().startswith(prefix_lower)]
        else:
            candidates = self._entries
        
        # Sort by frecency score (descending)
        candidates.sort(key=lambda e: e.frecency_score, reverse=True)
        
        return [e.text for e in candidates[:limit]]
    
    def fuzzy_search(self, query: str, limit: int = 10) -> list[str]:
        """Fuzzy search history.
        
        Args:
            query: Search query (fuzzy matched)
            limit: Maximum results to return
        
        Returns:
            List of matching prompts
        """
        if not query:
            return self.search(limit=limit)
        
        query_lower = query.lower()
        
        def fuzzy_match(text: str) -> bool:
            """Simple fuzzy match."""
            qi = 0
            for ch in text.lower():
                if qi < len(query_lower) and ch == query_lower[qi]:
                    qi += 1
            return qi == len(query_lower)
        
        candidates = [e for e in self._entries if fuzzy_match(e.text)]
        candidates.sort(key=lambda e: e.frecency_score, reverse=True)
        
        return [e.text for e in candidates[:limit]]
    
    def recent(self, limit: int = 10) -> list[str]:
        """Get most recent prompts.
        
        Args:
            limit: Maximum results to return
        
        Returns:
            List of recent prompts
        """
        sorted_entries = sorted(self._entries, key=lambda e: e.timestamp, reverse=True)
        return [e.text for e in sorted_entries[:limit]]
    
    def clear(self) -> None:
        """Clear all history."""
        self._entries = []
        self._save()
    
    def __len__(self) -> int:
        return len(self._entries)
    
    def __iter__(self) -> Iterator[str]:
        """Iterate over all prompts (most recent first)."""
        sorted_entries = sorted(self._entries, key=lambda e: e.timestamp, reverse=True)
        return iter(e.text for e in sorted_entries)


# Global history instance
_history: PromptHistory | None = None


def get_history() -> PromptHistory:
    """Get the global prompt history instance."""
    global _history
    if _history is None:
        _history = PromptHistory()
    return _history


def reset_history() -> None:
    """Reset the global history (for testing)."""
    global _history
    _history = None
