"""System prompt construction — template from file, variable interpolation, context gathering."""

from __future__ import annotations

import os
import platform
import subprocess
import sys
from pathlib import Path

from agents.memory.memory import build_memory_prompt_section
from agents.skills.skills import build_skill_descriptions
from .subagent import build_agent_descriptions
from agents.tools import get_deferred_tool_names

# ─── System prompt template (from file) ──────────────────────

_PROMPTS_DIR = Path(__file__).parent.parent / "prompts"

def _load_system_prompt_template() -> str:
    """Load system prompt template from file."""
    template_path = _PROMPTS_DIR / "system.txt"
    if template_path.exists():
        return template_path.read_text(encoding="utf-8")
    # Fallback to empty template if file not found
    return ""


import re as _re

# ─── @include resolution ─────────────────────────────────────
# Resolves @./path, @~/path, @/path references in CLAUDE.md files.

_INCLUDE_RE = _re.compile(r"^@(\./[^\s]+|~/[^\s]+|/[^\s]+)$", _re.MULTILINE)
_MAX_INCLUDE_DEPTH = 5


def _resolve_includes(
    content: str,
    base_path: Path,
    visited: set[str] | None = None,
    depth: int = 0,
) -> str:
    if depth >= _MAX_INCLUDE_DEPTH:
        return content
    if visited is None:
        visited = set()

    def _replace(m: _re.Match) -> str:
        raw = m.group(1)
        if raw.startswith("~/"):
            resolved = Path.home() / raw[2:]
        elif raw.startswith("/"):
            resolved = Path(raw)
        else:
            resolved = base_path / raw
        resolved = resolved.resolve()
        key = str(resolved)
        if key in visited:
            return f"<!-- circular: {raw} -->"
        if not resolved.is_file():
            return f"<!-- not found: {raw} -->"
        try:
            visited.add(key)
            included = resolved.read_text()
            return _resolve_includes(included, resolved.parent, visited, depth + 1)
        except Exception:
            return f"<!-- error reading: {raw} -->"

    return _INCLUDE_RE.sub(_replace, content)


def _load_rules_dir(directory: Path) -> str:
    """Load all .md files from .mycode/rules/ directory."""
    rules_dir = directory / ".mycode" / "rules"
    if not rules_dir.is_dir():
        return ""
    try:
        files = sorted(f for f in rules_dir.iterdir() if f.suffix == ".md" and f.is_file())
        if not files:
            return ""
        parts: list[str] = []
        for f in files:
            try:
                content = f.read_text()
                content = _resolve_includes(content, rules_dir)
                parts.append(f"<!-- rule: {f.name} -->\n{content}")
            except Exception:
                pass
        return "\n\n## Rules\n" + "\n\n".join(parts) if parts else ""
    except Exception:
        return ""


def load_claude_md() -> str:
    """Walk up from cwd collecting all CLAUDE.md files, resolving @includes."""
    parts: list[str] = []
    d = Path.cwd().resolve()
    while True:
        f = d / "CLAUDE.md"
        if f.is_file():
            try:
                content = f.read_text()
                content = _resolve_includes(content, d)
                parts.insert(0, content)
            except Exception:
                pass
        parent = d.parent
        if parent == d:
            break
        d = parent
    # Load .mycode/rules/*.md from cwd
    rules = _load_rules_dir(Path.cwd())
    claude_md = ""
    if parts:
        claude_md = "\n\n# Project Instructions (CLAUDE.md)\n" + "\n\n---\n\n".join(parts)
    return claude_md + rules


def get_git_context() -> str:
    """Get git branch, recent commits, and status."""
    try:
        opts = {"encoding": "utf-8", "timeout": 3, "capture_output": True}
        branch = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], **opts).stdout.strip()
        log = subprocess.run(["git", "log", "--oneline", "-5"], **opts).stdout.strip()
        status = subprocess.run(["git", "status", "--short"], **opts).stdout.strip()
        result = f"\nGit branch: {branch}"
        if log:
            result += f"\nRecent commits:\n{log}"
        if status:
            result += f"\nGit status:\n{status}"
        return result
    except Exception:
        return ""


def build_workspace_structure() -> str:
    """列出工作区第一层文件结构，作为环境信息发给模型。

    只列一层（目录带 / 后缀），跳过隐藏文件与常见噪音目录，
    最多 100 条，避免大仓库撑爆 prompt。
    """
    _SKIP = {"node_modules", "__pycache__", ".git", ".venv", "venv", "dist", "build", ".idea", ".DS_Store"}
    try:
        entries = []
        for p in sorted(Path.cwd().iterdir()):
            if p.name.startswith(".") or p.name in _SKIP:
                continue
            entries.append(p.name + "/" if p.is_dir() else p.name)
            if len(entries) >= 100:
                break
        if not entries:
            return ""
        return "\nWorkspace structure (top level):\n" + "\n".join(entries)
    except OSError:
        return ""


def build_system_prompt() -> str:
    """Build the full system prompt from template file + dynamic context."""
    from datetime import date
    today = date.today().isoformat()
    plat = f"{platform.system()} {platform.machine()}"
    shell = (os.environ.get("ComSpec") or "cmd.exe") if sys.platform == "win32" else os.environ.get("SHELL", "/bin/sh")
    git_context = get_git_context()
    claude_md = load_claude_md()
    memory_section = build_memory_prompt_section()
    skills_section = build_skill_descriptions()
    agent_section = build_agent_descriptions()
    workspace_structure = build_workspace_structure()

    deferred_names = get_deferred_tool_names()
    deferred_section = (
        f"\n\nThe following deferred tools are available via tool_search: {', '.join(deferred_names)}. Use tool_search to fetch their full schemas when needed."
        if deferred_names else ""
    )

    replacements = {
        "{{cwd}}": str(Path.cwd()),
        "{{date}}": today,
        "{{platform}}": plat,
        "{{shell}}": shell,
        "{{workspace_structure}}": workspace_structure,
        "{{git_context}}": git_context,
        "{{claude_md}}": claude_md,
        "{{memory}}": memory_section,
        "{{skills}}": skills_section,
        "{{agents}}": agent_section,
        "{{deferred_tools}}": deferred_section,
    }
    result = _load_system_prompt_template()
    for key, value in replacements.items():
        result = result.replace(key, value)
    return result
