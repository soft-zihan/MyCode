"""FP8 量化的**质量**检查：贪心解码一致性（I7b）。

背景：`infra/bench_matrix.py` 测出 FP8 相对 BF16 吞吐 +39%，但只有性能数字，
下不了「能不能上生产」的结论——量化会不会让模型说错话，是另一个问题。

为什么用「贪心解码一致性」而不是直接跑任务级评测：
- 任务级评测（如 MyCode 的 LOCA）在 0.8B/2B 这种小模型上**两臂都会触底**，
  测不出差异，只能得到「都很差」这个没有信息量的结论。
- 贪心解码（temperature=0）下，同一 prompt 的输出应当**逐字确定**。
  如果 FP8 与 BF16 的输出高度一致，说明量化没有改变模型的决策路径；
  如果大面积发散，说明精度损失已经影响到采样结果——这是质量退化的**上界证据**。
- 诚实边界：输出发散 ≠ 任务质量下降（可能只是同义改写），一致也 ≠ 任务质量无损。
  这是**第一道筛查**，不是终判。要终判必须在能力足够的模型上跑任务级评测。

用法（两步，分开是为了让两次采集各自独立、可重跑）：
    python infra/quant_quality.py collect --endpoint http://127.0.0.1:8123/v1 \\
        --model q2b --out data/infra/i7b/bf16.jsonl
    python infra/quant_quality.py collect ... --out data/infra/i7b/fp8.jsonl
    python infra/quant_quality.py compare --a data/infra/i7b/bf16.jsonl \\
        --b data/infra/i7b/fp8.jsonl --out data/infra/i7b/compare.md

依赖：仅标准库（urllib + difflib + concurrent.futures），所以 WSL 的 infra venv 和
项目 .venv 都能跑，不需要额外装东西。
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from difflib import SequenceMatcher
from pathlib import Path

DEFAULT_DATASET = Path("data/infra/bench_custom.jsonl")


@dataclass
class GenResult:
    index: int
    session_id: str
    text: str
    finish_reason: str
    prompt_tokens: int
    completion_tokens: int
    elapsed_s: float
    error: str = ""


def load_prompts(path: Path, limit: int) -> list[dict]:
    rows: list[dict] = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            rows.append(rec)
            if limit and len(rows) >= limit:
                break
    return rows


def build_payload(model: str, prompt: str, max_tokens: int) -> dict:
    """贪心解码：temperature=0 + 固定 seed，把随机性压到只剩数值精度这一项。"""
    return {
        "model": model,
        "prompt": prompt,
        "max_tokens": max_tokens,
        "temperature": 0.0,
        "top_p": 1.0,
        "seed": 1234,
        "stream": False,
    }


def call_completions(endpoint: str, payload: dict, timeout_s: float) -> dict:
    url = endpoint.rstrip("/") + "/completions"
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": "Bearer dummy"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        return json.loads(resp.read().decode("utf-8"))


def generate_one(
    endpoint: str, model: str, index: int, rec: dict, max_tokens: int, timeout_s: float
) -> GenResult:
    started = time.monotonic()
    try:
        data = call_completions(endpoint, build_payload(model, rec["prompt"], max_tokens), timeout_s)
        choice = data["choices"][0]
        usage = data.get("usage") or {}
        return GenResult(
            index=index,
            session_id=str(rec.get("session_id", "")),
            text=choice.get("text", ""),
            finish_reason=str(choice.get("finish_reason", "")),
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
            elapsed_s=round(time.monotonic() - started, 3),
        )
    except (urllib.error.URLError, urllib.error.HTTPError, KeyError, IndexError, json.JSONDecodeError) as exc:
        return GenResult(
            index=index,
            session_id=str(rec.get("session_id", "")),
            text="",
            finish_reason="error",
            prompt_tokens=0,
            completion_tokens=0,
            elapsed_s=round(time.monotonic() - started, 3),
            error=f"{type(exc).__name__}: {exc}",
        )


def collect(args: argparse.Namespace) -> int:
    dataset = Path(args.dataset)
    if not dataset.exists():
        print(f"数据集不存在：{dataset}（先跑 infra/build_bench_dataset.py）", file=sys.stderr)
        return 2
    recs = load_prompts(dataset, args.num_prompts)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)

    print(f"采集 {len(recs)} 条 → {out}（endpoint={args.endpoint} model={args.model}）")
    results: list[GenResult] = []
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = [
            pool.submit(generate_one, args.endpoint, args.model, i, rec, args.max_tokens, args.timeout)
            for i, rec in enumerate(recs)
        ]
        for fut in futures:
            results.append(fut.result())

    results.sort(key=lambda r: r.index)
    with out.open("w", encoding="utf-8") as fh:
        for r in results:
            fh.write(json.dumps(asdict(r), ensure_ascii=False) + "\n")

    errs = [r for r in results if r.error]
    print(f"完成 {len(results)} 条，失败 {len(errs)} 条")
    if errs:
        print(f"首个错误：{errs[0].error}", file=sys.stderr)
        return 1
    return 0


def first_divergence(a: str, b: str) -> int:
    """第一个不同字符的下标；完全相同返回 -1。"""
    if a == b:
        return -1
    for i, (x, y) in enumerate(zip(a, b)):
        if x != y:
            return i
    return min(len(a), len(b))


def load_results(path: Path) -> list[dict]:
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return sorted(rows, key=lambda r: r.get("index", 0))


def compare(args: argparse.Namespace) -> int:
    a_rows = load_results(Path(args.a))
    b_rows = load_results(Path(args.b))
    if len(a_rows) != len(b_rows):
        print(f"两侧条数不一致（{len(a_rows)} vs {len(b_rows)}），无法逐条对齐", file=sys.stderr)
        return 2
    if not a_rows:
        print("没有可比的数据", file=sys.stderr)
        return 2

    exact = 0
    ratios: list[float] = []
    div_positions: list[float] = []
    rows_md: list[str] = []
    for x, y in zip(a_rows, b_rows):
        ta, tb = x.get("text", ""), y.get("text", "")
        same = ta == tb
        exact += int(same)
        ratio = 1.0 if same else SequenceMatcher(None, ta, tb).ratio()
        ratios.append(ratio)
        div = first_divergence(ta, tb)
        if div >= 0:
            # 归一化：发散点落在输出的百分之几处。越靠后说明分歧越晚出现、影响越小。
            div_positions.append(div / max(1, max(len(ta), len(tb))))
        if len(rows_md) < args.show:
            sim_cell = "✅ 完全一致" if same else f"{ratio:.3f}"
            div_cell = "-" if div < 0 else f"{div}（{div / max(1, max(len(ta), len(tb))):.0%}）"
            rows_md.append(
                f"| {x.get('index')} | {str(x.get('session_id', ''))[:8]} | {len(ta)} | {len(tb)} | "
                f"{sim_cell} | {div_cell} |"
            )

    n = len(a_rows)
    lines = [
        f"# FP8 vs BF16 贪心解码一致性（I7b）",
        "",
        f"- A = `{args.a}`",
        f"- B = `{args.b}`",
        f"- 样本数 **{n}**，temperature=0、seed 固定、max_tokens 相同",
        "",
        "| 指标 | 值 | 怎么读 |",
        "|---|---|---|",
        f"| 完全一致条数 | **{exact}/{n}（{exact / n:.1%}）** | 逐字节相同的比例 |",
        f"| 平均字符相似度 | **{statistics.fmean(ratios):.4f}** | 1.0 = 完全一致 |",
        f"| 相似度中位数 | {statistics.median(ratios):.4f} | 抗离群 |",
        f"| 最低相似度 | {min(ratios):.4f} | 最差的一条 |",
        f"| 平均首发散位置 | {statistics.fmean(div_positions):.1%} | 分歧平均出现在输出的多靠后处 |"
        if div_positions
        else "| 平均首发散位置 | n/a | 全部一致 |",
        "",
        f"## 前 {len(rows_md)} 条明细",
        "",
        "| index | session | A 长度 | B 长度 | 相似度 | 首发散位置 |",
        "|---|---|---|---|---|---|",
        *rows_md,
        "",
        "## 判读纪律",
        "",
        "- 这是**筛查**不是终判：输出发散可能只是同义改写，一致也不等于任务质量无损。",
        "- 小模型（0.8B/2B）上任务级评测两臂都会触底，所以先用这个更敏感的信号。",
        "- 要下「能不能上生产」的结论，必须在能力足够的模型上跑任务级评测（如 MyCode 的 LOCA）。",
    ]
    text = "\n".join(lines) + "\n"
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        print(f"写入 {out}")
    print(text)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("collect", help="对一个端点采集贪心解码输出")
    c.add_argument("--endpoint", required=True, help="OpenAI 兼容 base url，如 http://127.0.0.1:8123/v1")
    c.add_argument("--model", required=True)
    c.add_argument("--dataset", default=str(DEFAULT_DATASET))
    c.add_argument("--num-prompts", type=int, default=40)
    c.add_argument("--max-tokens", type=int, default=128)
    c.add_argument("--concurrency", type=int, default=4)
    c.add_argument("--timeout", type=float, default=300.0)
    c.add_argument("--out", required=True)
    c.set_defaults(func=collect)

    m = sub.add_parser("compare", help="比较两次采集结果")
    m.add_argument("--a", required=True)
    m.add_argument("--b", required=True)
    m.add_argument("--out", default="")
    m.add_argument("--show", type=int, default=10)
    m.set_defaults(func=compare)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
