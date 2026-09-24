#!/usr/bin/env python3
"""PIL 截图标注模板：在已有图片上画框 + 标签。

用法：
  python annotate_image.py --image shot.png --out annotated.png \
      --annotations '[{"box": [x, y, w, h], "label": "问题区域", "color": "red", "width": 3}]'
  （--annotations 也可传 JSON 文件路径）

box 单位像素：[左上x, 左上y, 宽, 高]。成功打印 OUT=<绝对路径>。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--image", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--annotations", required=True, help="JSON 数组字符串或 JSON 文件路径")
    args = ap.parse_args()

    try:
        from PIL import Image, ImageDraw
    except ImportError:
        print("MISSING_DEP: Pillow。安装: .venv/bin/pip install pillow", file=sys.stderr)
        sys.exit(2)

    raw = args.annotations
    if Path(raw).exists():
        raw = Path(raw).read_text(encoding="utf-8")
    annotations = json.loads(raw)

    img = Image.open(args.image).convert("RGB")
    draw = ImageDraw.Draw(img)
    for ann in annotations:
        x, y, w, h = ann["box"]
        color = ann.get("color", "red")
        line_width = int(ann.get("width", 3))
        draw.rectangle([x, y, x + w, y + h], outline=color, width=line_width)
        label = ann.get("label")
        if label:
            ty = max(y - line_width - 14, 0)
            draw.rectangle([x, ty, x + draw.textlength(label) + 8, ty + 16], fill=color)
            draw.text((x + 4, ty + 2), label, fill="white")

    out = Path(args.out).expanduser().resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out)
    print(f"OUT={out}")


if __name__ == "__main__":
    main()
