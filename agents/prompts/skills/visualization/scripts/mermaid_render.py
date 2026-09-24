#!/usr/bin/env python3
"""mermaid 渲染模板：mermaid 源码 → png/svg（@mermaid-js/mermaid-cli）。

用法：
  python mermaid_render.py --code 'graph TD; A-->B;' --out out.png [--scale 2] [--theme default]
  python mermaid_render.py --mmd diagram.mmd --out out.svg

成功打印 OUT=<绝对路径>。mmdc 缺失时打印安装提示并以退出码 2 结束。
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
    src.add_argument("--code", help="mermaid 源码字符串")
    src.add_argument("--mmd", help="mermaid 源码文件路径")
    ap.add_argument("--out", required=True, help="输出图片路径（.png/.svg）")
    ap.add_argument("--scale", default="2")
    ap.add_argument("--theme", default="default")
    args = ap.parse_args()

    mmdc = shutil.which("mmdc")
    if not mmdc:
        print(
            "MISSING_DEP: mmdc（@mermaid-js/mermaid-cli）。安装: npm i -g @mermaid-js/mermaid-cli；"
            "无法安装时降级：把 mermaid 源码放进回复代码块展示。",
            file=sys.stderr,
        )
        sys.exit(2)

    code = args.code if args.code is not None else Path(args.mmd).read_text(encoding="utf-8")
    out = Path(args.out).expanduser().resolve()
    out.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.NamedTemporaryFile("w", suffix=".mmd", delete=False, encoding="utf-8") as f:
        f.write(code)
        mmd_path = f.name
    try:
        proc = subprocess.run(
            [mmdc, "-i", mmd_path, "-o", str(out), "-s", args.scale, "-t", args.theme,
             "-b", "transparent"],
            capture_output=True, text=True, timeout=120,
        )
    finally:
        Path(mmd_path).unlink(missing_ok=True)

    if proc.returncode != 0 or not out.exists():
        print(f"mermaid 渲染失败: {proc.stderr.strip()[:800]}", file=sys.stderr)
        sys.exit(1)
    print(f"OUT={out}")


if __name__ == "__main__":
    main()
