#!/usr/bin/env python3
"""graphviz 渲染模板：DOT 源码 → png/svg（系统 dot）。

用法：
  python graphviz_render.py --code 'digraph {a->b; b->c;}' --out out.png [--format png] [--layout dot]
  python graphviz_render.py --dot deps.dot --out out.svg

成功打印 OUT=<绝对路径>。dot 缺失时打印安装提示并以退出码 2 结束
（此时优先降级用 mermaid_render.py）。
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def main() -> None:
    ap = argparse.ArgumentParser()
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--code", help="DOT 源码字符串")
    src.add_argument("--dot", help="DOT 源码文件路径")
    ap.add_argument("--out", required=True, help="输出图片路径")
    ap.add_argument("--format", default="png", choices=["png", "svg", "pdf"])
    ap.add_argument("--layout", default="dot", help="布局引擎：dot/neato/fdp/sfdp/circo/twopi")
    args = ap.parse_args()

    dot_bin = shutil.which(args.layout) or shutil.which("dot")
    if not dot_bin:
        print(
            "MISSING_DEP: graphviz（dot）。安装: brew install graphviz / apt install graphviz；"
            "无法安装时降级用 mermaid_render.py。",
            file=sys.stderr,
        )
        sys.exit(2)

    code = args.code if args.code is not None else Path(args.dot).read_text(encoding="utf-8")
    out = Path(args.out).expanduser().resolve()
    out.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.NamedTemporaryFile("w", suffix=".dot", delete=False, encoding="utf-8") as f:
        f.write(code)
        dot_path = f.name
    try:
        proc = subprocess.run(
            [dot_bin, f"-T{args.format}", dot_path, "-o", str(out)],
            capture_output=True, text=True, timeout=120,
        )
    finally:
        Path(dot_path).unlink(missing_ok=True)

    if proc.returncode != 0 or not out.exists():
        print(f"graphviz 渲染失败: {proc.stderr.strip()[:800]}", file=sys.stderr)
        sys.exit(1)
    print(f"OUT={out}")


if __name__ == "__main__":
    main()

