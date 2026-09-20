"""outline_file 工具：文件结构大纲（结构化预读）。

设计对标 claude-code#34304 (mode:"map") / ast-outline / aider repo-map：
先拿带行号范围的大纲，再用 read_file(offset, limit) 精读片段。

覆盖三种解析器（Python ast / Markdown 标题 / TS-JS 声明扫描）
与工具注册链路（定义、只读白名单、并发白名单、子代理白名单、分发）。
"""

from __future__ import annotations

import pytest

from agents.tools.outline_tools import outline_file


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path


# ─── Python 解析器 ──────────────────────────────────────────

PY_SAMPLE = '''import os


CONSTANT = 1


class Base:
    """Base class docstring."""

    def __init__(self, x: int):
        self.x = x

    @property
    def value(self) -> int:
        return self.x


async def fetch(url: str) -> bytes:
    """Download something.

    Multi-line docstring body.
    """
    return b""


def helper(a, b=2, *args, **kwargs):
    pass
'''


def test_python_outline_classes_functions_ranges(workspace):
    (workspace / "sample.py").write_text(PY_SAMPLE, encoding="utf-8")
    result = outline_file({"file_path": "sample.py"})

    assert "Outline of sample.py" in result
    assert "(28 lines, python)" in result
    # 类与继承
    assert "class Base" in result
    # 方法缩进在类下、带行号范围
    assert "L10-11    def __init__(self, x: int)" in result
    # 装饰器 + 返回注解
    assert "@property def value(self) -> int" in result
    # async 函数与 docstring 首行
    assert "async def fetch(url: str) -> bytes" in result
    assert "# Download something." in result
    # 多行 docstring 只取首行
    assert "Multi-line docstring body" not in result
    assert "def helper(a, b=2, *args, **kwargs)" in result
    # 尾部提示配合 read_file 精读
    assert "Use read_file with offset/limit" in result


def test_python_line_ranges_point_to_real_code(workspace):
    (workspace / "sample.py").write_text(PY_SAMPLE, encoding="utf-8")
    result = outline_file({"file_path": "sample.py"})
    lines = PY_SAMPLE.split("\n")
    for line in result.split("\n"):
        if not line.startswith("L"):
            continue
        rng = line.split()[0]
        start, end = (int(x) for x in rng[1:].split("-"))
        assert 1 <= start <= end <= len(lines)
    # fetch 的行范围应覆盖 async def 到 return
    fetch_line = next(l for l in result.split("\n") if "async def fetch" in l)
    start, end = (int(x) for x in fetch_line.split()[0][1:].split("-"))
    assert lines[start - 1].startswith("async def fetch")
    assert lines[end - 1].strip() == 'return b""'


def test_python_syntax_error_returns_error(workspace):
    (workspace / "broken.py").write_text("def f(:\n", encoding="utf-8")
    result = outline_file({"file_path": "broken.py"})
    assert result.startswith("Error outlining file:")


def test_python_no_structural_elements(workspace):
    (workspace / "empty_ish.py").write_text("import os\nX = 1\n", encoding="utf-8")
    result = outline_file({"file_path": "empty_ish.py"})
    assert "No structural elements found" in result


# ─── Markdown 解析器 ────────────────────────────────────────

MD_SAMPLE = """# 标题

正文。

## 小节 A

```python
# 这不是标题
def fake(): pass
```

## 小节 B

### 子小节

结束。
"""


def test_markdown_headings_with_ranges(workspace):
    (workspace / "doc.md").write_text(MD_SAMPLE, encoding="utf-8")
    result = outline_file({"file_path": "doc.md"})

    assert "Outline of doc.md" in result
    assert "(17 lines, markdown)" in result
    assert "# 标题" in result
    assert "## 小节 A" in result
    assert "## 小节 B" in result
    assert "### 子小节" in result
    # fenced code block 内的伪标题必须被跳过
    assert "这不是标题" not in result
    # 小节 A 的范围终止于小节 B 之前
    a_line = next(l for l in result.split("\n") if "小节 A" in l)
    start, end = (int(x) for x in a_line.split()[0][1:].split("-"))
    assert start == 5
    assert end == 11


# ─── TypeScript / JavaScript 解析器 ─────────────────────────

TS_SAMPLE = """import { x } from './x';

export interface Props {
  name: string;
}

type Handler = (e: Event) => void;

export class Player {
  private hp: number;

  constructor(hp: number) {
    this.hp = hp;
  }

  takeDamage(n: number) {
    if (n > 0) {
      this.hp -= n;
    }
  }
}

export function heal(p: Player) {
  p.takeDamage(-1);
}

const compute = (a: number) => a * 2;
"""


def test_typescript_outline(workspace):
    (workspace / "player.tsx").write_text(TS_SAMPLE, encoding="utf-8")
    result = outline_file({"file_path": "player.tsx"})

    assert "(28 lines, typescript)" in result
    assert "export interface Props {" in result
    assert "export class Player {" in result
    assert "constructor(hp: number) {" in result
    assert "takeDamage(n: number) {" in result
    assert "export function heal(p: Player) {" in result
    assert "const compute = (a: number) => a * 2;" in result
    # class 行范围覆盖整个类体
    cls_line = next(l for l in result.split("\n") if "class Player" in l)
    start, end = (int(x) for x in cls_line.split()[0][1:].split("-"))
    assert start == 9
    assert end == 21


def test_javascript_strings_with_braces_do_not_break_ranges(workspace):
    (workspace / "tricky.js").write_text(
        'function f() {\n  const s = "}{";\n  return s;\n}\n', encoding="utf-8"
    )
    result = outline_file({"file_path": "tricky.js"})
    f_line = next(l for l in result.split("\n") if "function f" in l)
    start, end = (int(x) for x in f_line.split()[0][1:].split("-"))
    assert (start, end) == (1, 4)


# ─── 不支持的类型与错误 ─────────────────────────────────────

def test_unsupported_extension_returns_error(workspace):
    (workspace / "data.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    result = outline_file({"file_path": "data.csv"})
    assert result.startswith("Error:")
    assert ".py" in result and ".md" in result


def test_missing_file_returns_error(workspace):
    result = outline_file({"file_path": "nope.py"})
    assert result.startswith("Error outlining file:")


# ─── 注册链路 ───────────────────────────────────────────────

def test_tool_registered_everywhere():
    from agents.tools.registry import (
        tool_definitions,
        READ_TOOLS,
        CONCURRENCY_SAFE_TOOLS,
        TOOL_EXECUTION_MODES,
    )
    from agents.core.subagent import READ_ONLY_TOOLS

    names = [t["name"] for t in tool_definitions]
    assert "outline_file" in names
    assert "outline_file" in READ_TOOLS
    assert "outline_file" in CONCURRENCY_SAFE_TOOLS
    assert "outline_file" in READ_ONLY_TOOLS
    assert TOOL_EXECUTION_MODES["outline_file"] == "parallel"


def test_permission_auto_allows_outline(workspace):
    from agents.tools.permissions import check_permission

    assert check_permission("outline_file", {"file_path": "x.py"})["action"] == "allow"


async def test_execute_tool_dispatch(workspace):
    from agents.tools import execute_tool

    (workspace / "sample.py").write_text(PY_SAMPLE, encoding="utf-8")
    result = await execute_tool("outline_file", {"file_path": "sample.py"})
    assert "class Base" in result
