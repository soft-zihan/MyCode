"""Outline Tools - 文件结构大纲（结构化预读）。

outline_file 返回文件的结构大纲（每个条目带行号范围），模型先看大纲、
再用 read_file(offset, limit) 精读目标片段，避免把大文件整体读进上下文。
业界同款设计：claude-code#34304 的 mode:"map" 提案、ast-outline、aider repo-map。

解析器选型（按本项目语言构成，零新增依赖）：
- Python: 标准库 ast，精确到装饰器/async/继承/docstring 首行
- Markdown: ATX 标题解析，跳过 fenced code block 内的伪标题
- TypeScript/JavaScript: 声明行正则 + 字符串/注释感知的花括号配对定界
"""

from __future__ import annotations

import ast
import re

from agents.tools.paths import resolve_tool_path

_LANG_BY_EXT = {
    ".py": "python",
    ".md": "markdown",
    ".markdown": "markdown",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "javascript",
    ".jsx": "javascript",
}

_MAX_ENTRIES = 300
_MAX_SIG_CHARS = 110
_MAX_DOC_CHARS = 70

# (start, end, depth, text)
Entry = tuple[int, int, int, str]


def outline_file(inp: dict) -> str:
    try:
        from agents.tools.runtime import get_runtime, DockerRuntime
        rt = get_runtime()

        if isinstance(rt, DockerRuntime):
            path_str = inp["file_path"]
        else:
            path_str = str(resolve_tool_path(inp["file_path"]))

        name = path_str.rsplit("/", 1)[-1]
        ext = "." + name.rsplit(".", 1)[-1].lower() if "." in name else ""
        lang = _LANG_BY_EXT.get(ext)
        if lang is None:
            supported = ", ".join(sorted(_LANG_BY_EXT))
            return (
                f"Error: outline_file does not support '{ext or name}' files "
                f"(supported extensions: {supported}). "
                "Use grep_search or read_file for this file."
            )

        content = rt.read_file(path_str)

        if lang == "python":
            entries = _outline_python(content)
        elif lang == "markdown":
            entries = _outline_markdown(content)
        else:
            entries = _outline_ts(content, lang)

        total_lines = content.count("\n") + 1
        header = f"Outline of {inp['file_path']} ({total_lines} lines, {lang})"
        if not entries:
            return f"{header}\n\nNo structural elements found. Use read_file to read it directly."

        truncated = ""
        if len(entries) > _MAX_ENTRIES:
            truncated = f"\n... and {len(entries) - _MAX_ENTRIES} more elements"
            entries = entries[:_MAX_ENTRIES]

        range_width = max(len(f"L{e[0]}-{e[1]}") for e in entries)
        body = "\n".join(
            f"L{start}-{end}".ljust(range_width) + "  " + "  " * depth + text
            for start, end, depth, text in entries
        )
        return (
            f"{header}\n\n{body}{truncated}\n\n"
            "Use read_file with offset/limit to read a specific section."
        )
    except Exception as e:
        return f"Error outlining file: {e}"


# ─── Python (ast) ───────────────────────────────────────────

_METHOD_DECORATORS = {"property", "staticmethod", "classmethod", "abstractmethod"}


def _py_start_line(node: ast.AST) -> int:
    decorators = getattr(node, "decorator_list", [])
    if decorators:
        return decorators[0].lineno - 1
    return node.lineno


def _py_signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    kind = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
    try:
        args = ast.unparse(node.args)
    except Exception:
        args = "..."
    ret = ""
    if node.returns is not None:
        try:
            ret = f" -> {ast.unparse(node.returns)}"
        except Exception:
            ret = ""
    sig = f"{kind} {node.name}({args}){ret}"
    if len(sig) > _MAX_SIG_CHARS:
        sig = sig[:_MAX_SIG_CHARS - 1] + "…"
    decorators = getattr(node, "decorator_list", [])
    for d in decorators:
        dname = d.id if isinstance(d, ast.Name) else (d.attr if isinstance(d, ast.Attribute) else "")
        if dname in _METHOD_DECORATORS:
            sig = f"@{dname} {sig}"
            break
    return sig


def _py_doc(node: ast.AST) -> str:
    try:
        doc = ast.get_docstring(node)
    except Exception:
        return ""
    if not doc:
        return ""
    first = doc.strip().split("\n")[0]
    if len(first) > _MAX_DOC_CHARS:
        first = first[:_MAX_DOC_CHARS - 1] + "…"
    return f"  # {first}"


def _outline_python(content: str) -> list[Entry]:
    tree = ast.parse(content)
    entries: list[Entry] = []

    def walk(body: list[ast.stmt], depth: int) -> None:
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                entries.append((_py_start_line(node), node.end_lineno, depth, _py_signature(node) + _py_doc(node)))
            elif isinstance(node, ast.ClassDef):
                bases = ", ".join(ast.unparse(b) for b in node.bases)
                header = f"class {node.name}" + (f"({bases})" if bases else "")
                entries.append((_py_start_line(node), node.end_lineno, depth, header + _py_doc(node)))
                walk(node.body, depth + 1)

    walk(tree.body, 0)
    return entries


# ─── Markdown (ATX headings) ────────────────────────────────

_MD_HEADING = re.compile(r"^(#{1,6})\s+(.+?)(?:\s+#+)?$")
_MD_FENCE = re.compile(r"^(`{3,}|~{3,})")


def _outline_markdown(content: str) -> list[Entry]:
    lines = content.split("\n")
    headings: list[tuple[int, int, str]] = []  # (line, level, text)
    fence: str | None = None
    for i, line in enumerate(lines, 1):
        stripped = line.strip()
        m_fence = _MD_FENCE.match(stripped)
        if m_fence:
            marker = m_fence.group(1)
            if fence is None:
                fence = marker
            elif marker[0] == fence[0] and len(marker) >= len(fence):
                fence = None
            continue
        if fence is not None:
            continue
        m = _MD_HEADING.match(line)
        if m:
            headings.append((i, len(m.group(1)), m.group(2).strip()))

    entries: list[Entry] = []
    for idx, (line, level, text) in enumerate(headings):
        end = len(lines)
        for next_line, next_level, _ in headings[idx + 1:]:
            if next_level <= level:
                end = next_line - 1
                break
        entries.append((line, end, level - 1, "#" * level + " " + text))
    return entries


# ─── TypeScript / JavaScript ────────────────────────────────

_TS_CLASS_DECL = re.compile(r"^(?:export\s+)?(?:declare\s+)?(?:abstract\s+)?class\s+(\w+)")
_TS_TOP_DECL = re.compile(
    r"^(?:export\s+)?(?:declare\s+)?(?:default\s+)?"
    r"(?:"
    r"(?:async\s+)?function\s*\*?\s*(\w+)"
    r"|(?:interface|type|enum|namespace)\s+(\w+)"
    r"|const\s+(\w+)(?::[^=]+)?\s*=\s*(?:async\s*)?(?:\(|function\b|[\w.<>\[\]]*\s*=>)"
    r")"
)
_TS_CLASS_MEMBER = re.compile(
    r"^\s+(?:(?:public|private|protected|static|readonly|abstract|override|async|get|set)\s+)*"
    r"(?:constructor\s*[(<]|(\w+)\s*[(<]|(\w+)\s*=\s*(?:async\s*)?\()"
)


def _ts_strip_code(line: str, state: dict) -> str:
    """去掉字符串与注释内容，保留花括号计数所需的代码骨架。"""
    out = []
    i, n = 0, len(line)
    while i < n:
        c = line[i]
        if state["block_comment"]:
            if c == "*" and i + 1 < n and line[i + 1] == "/":
                state["block_comment"] = False
                i += 2
            else:
                i += 1
            continue
        if state["quote"] is not None:
            if c == "\\":
                i += 2
                continue
            if c == state["quote"]:
                state["quote"] = None
            i += 1
            continue
        if c == "/" and i + 1 < n and line[i + 1] == "/":
            break
        if c == "/" and i + 1 < n and line[i + 1] == "*":
            state["block_comment"] = True
            i += 2
            continue
        if c in "\"'`":
            state["quote"] = c
            i += 1
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _ts_end(cleaned: list[str], start: int) -> int:
    """从声明行开始做花括号配对，返回声明结束行（0-based）。"""
    depth = 0
    seen_brace = False
    for j in range(start, len(cleaned)):
        depth += cleaned[j].count("{") - cleaned[j].count("}")
        if depth > 0:
            seen_brace = True
        if seen_brace and depth <= 0:
            return j
        if not seen_brace and cleaned[j].rstrip().endswith(";"):
            return j
    return len(cleaned) - 1


def _ts_label(line: str, name: str, kind: str) -> str:
    label = line.strip()
    if len(label) > _MAX_SIG_CHARS:
        label = label[:_MAX_SIG_CHARS - 1] + "…"
    return label if label else f"{kind} {name}"


def _outline_ts(content: str, lang: str) -> list[Entry]:
    lines = content.split("\n")
    state = {"quote": None, "block_comment": False}
    cleaned = [_ts_strip_code(line, state) for line in lines]

    entries: list[Entry] = []
    depth = 0
    class_end: int | None = None  # 当前顶层 class 的结束行（0-based），其 body 内扫描成员
    for i, cl in enumerate(cleaned):
        depth_before = depth
        depth += cl.count("{") - cl.count("}")

        if depth_before == 0:
            m = _TS_CLASS_DECL.match(cl)
            if m:
                end = _ts_end(cleaned, i)
                entries.append((i + 1, end + 1, 0, _ts_label(lines[i], m.group(1), "class")))
                class_end = end
                continue
            m = _TS_TOP_DECL.match(cl)
            if m:
                name = next(g for g in m.groups() if g)
                end = _ts_end(cleaned, i)
                entries.append((i + 1, end + 1, 0, _ts_label(lines[i], name, "decl")))
                class_end = None
                continue
            class_end = None

        if class_end is not None and depth_before == 1 and i < class_end:
            m = _TS_CLASS_MEMBER.match(cl)
            if m:
                name = m.group(1) or m.group(2) or "constructor"
                end = min(_ts_end(cleaned, i), class_end)
                entries.append((i + 1, end + 1, 1, _ts_label(lines[i], name, "member")))
    return entries
