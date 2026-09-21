"""System prompt construction — template from file, variable interpolation, context gathering."""

from __future__ import annotations

import os
import platform
import subprocess
import sys
from pathlib import Path

from agents.core.workspace import get_workspace
from agents.wiki.wiki_manager import build_wiki_prompt_section, init_wiki_git
from agents.skills.skills import build_skill_descriptions
from .subagent import build_agent_descriptions

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
    """Walk up from the workspace collecting all CLAUDE.md files, resolving @includes."""
    parts: list[str] = []
    d = get_workspace().resolve()
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
    rules = _load_rules_dir(get_workspace())
    claude_md = ""
    if parts:
        claude_md = "\n\n# Project Instructions (CLAUDE.md)\n" + "\n\n---\n\n".join(parts)
    rules_md = _load_rules_md(get_workspace())
    return claude_md + rules + rules_md


def load_agents_md() -> str:
    """Walk up from the workspace collecting all AGENTS.md files, resolving @includes."""
    parts: list[str] = []
    d = get_workspace().resolve()
    while True:
        f = d / "AGENTS.md"
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
    if parts:
        return "\n\n# Agent Instructions (AGENTS.md)\n" + "\n\n---\n\n".join(parts)
    return ""


def _load_rules_md(directory: Path) -> str:
    """Load .mycode/RULES.md — always-on project rules."""
    rules_file = directory / ".mycode" / "RULES.md"
    if not rules_file.is_file():
        return ""
    try:
        content = rules_file.read_text(encoding="utf-8")
        content = _resolve_includes(content, rules_file.parent)
        return "\n\n# Project Rules (RULES.md)\n\n" + content
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
        for p in sorted(get_workspace().iterdir()):
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
    import time
    from datetime import date
    _t0 = time.perf_counter()
    today = date.today().isoformat()
    plat = f"{platform.system()} {platform.machine()}"
    shell = (os.environ.get("ComSpec") or "cmd.exe") if sys.platform == "win32" else os.environ.get("SHELL", "/bin/sh")

    _t2 = time.perf_counter()
    claude_md = load_claude_md()
    _claude_md_ms = (time.perf_counter() - _t2) * 1000

    _t2b = time.perf_counter()
    agents_md = load_agents_md()
    _agents_md_ms = (time.perf_counter() - _t2b) * 1000

    _t4 = time.perf_counter()
    wiki_section = build_wiki_prompt_section()
    _wiki_ms = (time.perf_counter() - _t4) * 1000

    _t5 = time.perf_counter()
    skills_section = build_skill_descriptions()
    _skills_ms = (time.perf_counter() - _t5) * 1000

    _t6 = time.perf_counter()
    agent_section = build_agent_descriptions()
    _agents_ms = (time.perf_counter() - _t6) * 1000

    workspace_structure = build_workspace_structure()

    init_wiki_git()

    replacements = {
        "{{cwd}}": str(get_workspace()),
        "{{date}}": today,
        "{{platform}}": plat,
        "{{shell}}": shell,
        "{{workspace_structure}}": workspace_structure,
        "{{claude_md}}": claude_md,
        "{{agents_md}}": agents_md,
        "{{wiki}}": wiki_section,
        "{{skills}}": skills_section,
        "{{agents}}": agent_section,
        "{{deferred_tools}}": "",
    }
    result = _load_system_prompt_template()
    for key, value in replacements.items():
        result = result.replace(key, value)

    _total_ms = (time.perf_counter() - _t0) * 1000
    if _total_ms > 50:
        print(
            f"[perf] build_system_prompt: {_total_ms:.1f}ms "
            f"(claude_md={_claude_md_ms:.1f} agents_md={_agents_md_ms:.1f} "
            f"wiki={_wiki_ms:.1f} "
            f"skills={_skills_ms:.1f} agents={_agents_ms:.1f})",
            file=sys.stderr,
        )

    return result
