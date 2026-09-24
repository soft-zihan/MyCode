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


# U5a：工具指引按工具存在性条件生成（v2 system-prompt.ts OPENCODE_TOOL_GUIDANCE
# 模式——"有 shell 才注入 shell 纪律"）。active_tools=None 时全量注入。
_DEDICATED_TOOL_LINES = [
    ("read_file", "   - 读取文件使用 read_file 而不是 cat、head、tail 或 sed"),
    ("outline_file", "   - 对于大型代码/markdown 文件（>300 行），先调用 outline_file 获取结构大纲（类/函数/标题及行号范围），然后通过 offset/limit 只读取你需要的部分"),
    ("edit_file", "   - 编辑文件使用 edit_file 而不是 sed 或 awk"),
    ("write_file", "   - 创建文件使用 write_file 而不是 cat 与 heredoc 或 echo 重定向"),
    ("list_files", "   - 搜索文件使用 list_files 而不是 find 或 ls"),
    ("grep_search", "    - 搜索文件内容使用 grep_search 而不是 grep 或 rg"),
    ("web_search", "    - 搜索公网事实、文档、URL、日期或最新信息使用 web_search，而不是 curl、wget 或手写搜索脚本"),
]

_CODE_GRAPH_PREFIX = "mcp__code-review-graph__"


def build_tool_guidance(active_tools: set[str] | None = None) -> str:
    """生成"# 使用工具"段：每条指引以其对应工具存在为前提。"""
    def has(name: str) -> bool:
        return active_tools is None or name in active_tools

    lines = ["# 使用工具"]
    if has("run_shell"):
        dedicated = [text for name, text in _DEDICATED_TOOL_LINES if has(name)]
        if dedicated:
            lines.append(" - 当有相关的专用工具时，不要使用 run_shell 运行命令。使用专用工具让用户更好地理解和审查你的工作。这对于帮助你至关重要：")
            lines.extend(dedicated)
        lines.append("    - 仅在需要 shell 执行的系统命令和终端操作上专门使用 run_shell。如果你不确定并且有相关的专用工具，默认使用专用工具，只有在绝对必要时才使用 run_shell 工具。")
    lines.append(" - 你可以在一次响应中调用多个工具。如果你打算调用多个工具且它们之间没有依赖关系，并行执行所有独立的工具调用。尽可能最大化使用并行工具调用以提高效率。但是，如果某些工具调用依赖于之前的调用来确定依赖值，不要并行调用这些工具，而是按顺序调用它们。例如，如果一个操作必须在另一个操作开始之前完成，按顺序运行这些操作而不是并行。")
    if has("agent"):
        lines.append(" - 当任务与 agent 的描述匹配时，使用 `agent` 工具调用专门的 agent。子 agent 对于并行化独立查询或保护主上下文窗口免受过多结果影响很有价值，但不应该在不需要时过度使用。重要的是，避免重复子 agent 已经在做的工作——如果你将研究委托给子 agent，不要自己也执行相同的搜索。子 agent 默认不注入 wiki 记忆——派发任务时，把任务所需的相关记忆/约定/教训显式写进任务描述（prompt）里。")
    if active_tools is None or any(n.startswith(_CODE_GRAPH_PREFIX) for n in active_tools):
        lines.extend([
            " - 代码图（如果已连接）：当名为 `mcp__code-review-graph__*` 的 MCP 工具可用时，存在代码库的结构知识图（由用户通过 /graph build 构建）。用它来回答多跳结构问题，而不是广泛的 grep：",
            "   - \"谁调用 X / X 调用什么 / 谁导入模块 Y\" → `mcp__code-review-graph__query_graph_tool`（模式：callers_of、callees_of、importers_of、imports_of、tests_for、inheritors_of）",
            "   - \"更改文件 X 会影响什么\" → `mcp__code-review-graph__get_impact_radius_tool` 或 `mcp__code-review-graph__detect_changes_tool`",
            "   - 按名称查找符号 → `mcp__code-review-graph__semantic_search_nodes_tool`",
            "   对于一跳查找（精确字符串、文件通配符），继续使用 grep_search/list_files。如果图查询报告图缺失或过时，告诉用户运行 /graph build 或 /graph update——不要自己构建它。",
        ])
    return "\n".join(lines)


def build_system_prompt(active_tools: set[str] | None = None) -> str:
    """Build the full system prompt from template file + dynamic context.

    active_tools：当前工具名快照，用于条件化工具指引（None = 全量注入）。
    """
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

    workspace_structure = build_workspace_structure()

    init_wiki_git()

    replacements = {
        "{{cwd}}": str(get_workspace()),
        "{{date}}": today,
        "{{platform}}": plat,
        "{{shell}}": shell,
        "{{tool_guidance}}": build_tool_guidance(active_tools),
        "{{workspace_structure}}": workspace_structure,
        "{{claude_md}}": claude_md,
        "{{agents_md}}": agents_md,
        "{{wiki}}": wiki_section,
        "{{skills}}": skills_section,
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
            f"skills={_skills_ms:.1f})",
            file=sys.stderr,
        )

    return result
