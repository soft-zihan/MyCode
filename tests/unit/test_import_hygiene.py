"""M4 依赖治理门禁：agents/ 内禁止函数级 agents import 与模块级循环依赖。

背景：函数内延迟 import 是绕循环依赖的典型产物（峰值 196 处）。M4 解环
（retry.py / agent_cls 注入 / provider 倒置 / wiki store 叶子层）+ 机械化上提后
基线清零——本测试冻结该状态，新增延迟 import 或模块级环都会在此失败。

例外必须走这里显式豁免（附理由），不允许静默新增。
"""
import ast
import os
from collections import defaultdict

ROOT = os.path.join(os.path.dirname(__file__), "..", "..", "agents")

# (文件相对路径, 被 import 的 agents 模块) → 豁免理由
LAZY_IMPORT_EXEMPTIONS: dict[tuple[str, str], str] = {}


def _py_files():
    for dirpath, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for f in files:
            if f.endswith(".py"):
                yield os.path.join(dirpath, f)


def _rel(path):
    return os.path.relpath(path, os.path.join(ROOT, ".."))


def test_no_function_level_agents_imports():
    violations = []
    for p in _py_files():
        try:
            tree = ast.parse(open(p).read())
        except SyntaxError:
            continue
        for fn in (n for n in ast.walk(tree)
                   if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))):
            for node in ast.walk(fn):
                if node is fn:
                    continue
                mods = []
                if isinstance(node, ast.ImportFrom) and node.module:
                    mods = [node.module] if node.module.startswith("agents") else []
                elif isinstance(node, ast.Import):
                    mods = [a.name for a in node.names if a.name.startswith("agents")]
                for m in mods:
                    if (_rel(p), m) in LAZY_IMPORT_EXEMPTIONS:
                        continue
                    violations.append(f"{_rel(p)}:{node.lineno} -> {m}")
    assert not violations, (
        "函数级 agents import 复发（循环依赖的信号）——请解环而不是延迟 import；"
        "确有理由请加入 LAZY_IMPORT_EXEMPTIONS 并附说明：\n" + "\n".join(violations)
    )


def test_no_module_level_dependency_cycles():
    edges = defaultdict(set)

    def modname(path):
        return os.path.relpath(path, os.path.join(ROOT, ".."))[:-3].replace("/", ".")

    for p in _py_files():
        try:
            tree = ast.parse(open(p).read())
        except SyntaxError:
            continue
        m = modname(p)
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("agents"):
                edges[m].add(node.module)
            elif isinstance(node, ast.Import):
                for a in node.names:
                    if a.name.startswith("agents"):
                        edges[m].add(a.name)

    # Tarjan SCC（迭代版，避免深递归）
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    onstack: set[str] = set()
    stack: list[str] = []
    sccs = []
    nodes = set(edges) | {t for v in edges.values() for t in v}

    for root in sorted(nodes):
        if root in index:
            continue
        work = [(root, iter(sorted(edges.get(root, ()))))]
        index[root] = low[root] = len(index)
        stack.append(root)
        onstack.add(root)
        while work:
            v, it = work[-1]
            advanced = False
            for w in it:
                if w not in index:
                    index[w] = low[w] = len(index)
                    stack.append(w)
                    onstack.add(w)
                    work.append((w, iter(sorted(edges.get(w, ())))))
                    advanced = True
                    break
                elif w in onstack:
                    low[v] = min(low[v], index[w])
            if advanced:
                continue
            work.pop()
            if work:
                low[work[-1][0]] = min(low[work[-1][0]], low[v])
            if low[v] == index[v]:
                comp = []
                while True:
                    w = stack.pop()
                    onstack.discard(w)
                    comp.append(w)
                    if w == v:
                        break
                if len(comp) > 1:
                    sccs.append(sorted(comp))

    assert not sccs, "agents/ 出现模块级循环依赖：\n" + "\n".join(map(str, sccs))
