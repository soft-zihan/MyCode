#!/usr/bin/env python3
"""matplotlib 图表模板：line / bar / scatter / heatmap。

用法：
  python matplotlib_chart.py --type bar --data chart.json --out out.png \
      [--title T] [--xlabel X] [--ylabel Y] [--width 10] [--height 6] [--dpi 150]

data JSON：
  line/bar : {"labels": [...], "series": [{"name": "s1", "values": [...]}]}
             （series 可省 name；也接受 {"labels": [...], "values": [...]}）
  scatter  : {"series": [{"name": "s1", "values": [[x, y], ...]}]}
  heatmap  : {"matrix": [[...]], "x_labels": [...], "y_labels": [...]}

成功打印 OUT=<绝对路径>。依赖缺失时打印安装提示并以退出码 2 结束。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _require_matplotlib():
    try:
        import matplotlib  # noqa: F401
    except ImportError:
        print("MISSING_DEP: matplotlib。安装后重试，如: .venv/bin/pip install matplotlib", file=sys.stderr)
        sys.exit(2)
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib import font_manager

    # 中文标签：按平台常见 CJK 字体设置候选链
    available = {f.name for f in font_manager.fontManager.ttflist}
    for cand in ("Arial Unicode MS", "PingFang SC", "Hiragino Sans GB",
                 "Noto Sans CJK SC", "Source Han Sans SC", "Microsoft YaHei"):
        if cand in available:
            plt.rcParams["font.family"] = [cand, "sans-serif"]
            break
    plt.rcParams["axes.unicode_minus"] = False
    return plt


def _norm_series(data: dict) -> list[dict]:
    if "series" in data:
        series = data["series"]
        return [s if isinstance(s, dict) else {"values": s} for s in series]
    if "values" in data:
        return [{"values": data["values"]}]
    raise SystemExit("data 需包含 series 或 values 字段")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--type", required=True, choices=["line", "bar", "scatter", "heatmap"])
    ap.add_argument("--data", required=True, help="JSON 数据文件路径")
    ap.add_argument("--out", required=True, help="输出图片路径（png）")
    ap.add_argument("--title", default="")
    ap.add_argument("--xlabel", default="")
    ap.add_argument("--ylabel", default="")
    ap.add_argument("--width", type=float, default=10.0)
    ap.add_argument("--height", type=float, default=6.0)
    ap.add_argument("--dpi", type=int, default=150)
    args = ap.parse_args()

    plt = _require_matplotlib()
    data = json.loads(Path(args.data).read_text(encoding="utf-8"))

    fig, ax = plt.subplots(figsize=(args.width, args.height), dpi=args.dpi)
    if args.type == "heatmap":
        im = ax.imshow(data["matrix"], cmap="viridis", aspect="auto")
        fig.colorbar(im, ax=ax)
        if data.get("x_labels"):
            ax.set_xticks(range(len(data["x_labels"])), data["x_labels"], rotation=45, ha="right")
        if data.get("y_labels"):
            ax.set_yticks(range(len(data["y_labels"])), data["y_labels"])
    else:
        labels = data.get("labels")
        series = _norm_series(data)
        for i, s in enumerate(series):
            name = s.get("name") or f"series{i + 1}"
            values = s["values"]
            if args.type == "line":
                ax.plot(labels if labels else range(len(values)), values, marker="o", label=name)
            elif args.type == "bar":
                width = 0.8 / max(len(series), 1)
                xs = [j + (i - (len(series) - 1) / 2) * width for j in range(len(values))]
                ax.bar(xs, values, width=width, label=name)
                if labels:
                    ax.set_xticks(range(len(labels)), labels)
            elif args.type == "scatter":
                pts = [tuple(p) for p in values]
                ax.scatter([p[0] for p in pts], [p[1] for p in pts], label=name, alpha=0.75)
        if len(series) > 1 or series[0].get("name"):
            ax.legend()
    if args.title:
        ax.set_title(args.title)
    if args.xlabel:
        ax.set_xlabel(args.xlabel)
    if args.ylabel:
        ax.set_ylabel(args.ylabel)
    ax.grid(True, alpha=0.3)
    fig.tight_layout()

    out = Path(args.out).expanduser().resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out)
    plt.close(fig)
    print(f"OUT={out}")


if __name__ == "__main__":
    main()
