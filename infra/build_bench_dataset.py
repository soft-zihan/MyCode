"""MyCode 真实 agent 流量 → vLLM 压测数据集（custom JSONL）。

输入：``data/rl/samples.jsonl``（由 rl/export_sessions.py 产出，取 kind="tool_call"）。
输出：``{"prompt", "output_tokens", "session_id", "sample_index"}`` 的 JSONL，
供 ``vllm bench serve --dataset-name custom --dataset-path ... --disable-shuffle``。

为什么用 custom 而不是 sharegpt：sharegpt 走 ``is_valid_sequence`` 的默认硬过滤
（max_prompt_len=1024、max_total_len=2048 token），真实 agent 的 prompt 普遍 1-4K token，
会被**整批静默丢掉**；custom 不做长度过滤，且 ``--disable-shuffle`` 能保住我们的请求顺序。

为什么顺序要紧：agent 多轮请求天然是「长共享前缀 + 短输出」——同一会话里后一次请求
的 prompt 包含前一次的全部内容，正是 prefix caching 的甜区。所以输出**按 session 分组、
组内按 sample_index 递增**排列；一旦被打乱，缓存命中率就不再反映真实 agent 流量。

口径说明：prompt 是 state 的**文本序列化**（role 标签 + 内容拼接），用于塑造负载形状，
不等于线上真实 prompt（真实请求还含冻结的系统提示词与工具 schema）。因此本数据集
用于**开关之间的相对比较**，绝对吞吐不代表 MyCode 线上表现。

samples.jsonl 由 feature/agent-rl-local 分支的导出器产出（data/ 已 gitignore，跨分支复用产物）。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

DEFAULT_INPUT = "data/rl/samples.jsonl"
DEFAULT_OUTPUT = "data/infra/bench_custom.jsonl"
# 压测时输出长度由 --custom-output-len 统一固定（消除输出长度方差对延迟对比的干扰），
# 这里的 output_tokens 只是备用估计：按 4 字符≈1 token 粗算，下限 16。
CHARS_PER_TOKEN = 4
MIN_OUTPUT_TOKENS = 16


def load_tool_call_samples(path: Path) -> list[dict[str, Any]]:
    """读导出产物，只保留 tool_call 样本（坏行跳过）。"""
    samples: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if record.get("kind") == "tool_call":
                samples.append(record)
    return samples


def state_to_prompt(state: list[dict[str, Any]], max_chars: int = 0) -> str:
    """把一段会话历史序列化成单条 prompt 文本（保留 tool_calls 以便负载形状接近真实）。"""
    parts: list[str] = []
    for message in state:
        role = message.get("role", "user")
        content = message.get("content") or ""
        if message.get("tool_calls"):
            calls = json.dumps(message["tool_calls"], ensure_ascii=False)
            content = f"{content}\n{calls}" if content else calls
        parts.append(f"<|{role}|>\n{content}")
    prompt = "\n\n".join(parts)
    if max_chars and len(prompt) > max_chars:
        # 从头部截断（保留最近的上下文），与导出器的 trim_state 口径一致
        prompt = prompt[-max_chars:]
    return prompt


def to_custom_record(sample: dict[str, Any], max_chars: int = 0) -> dict[str, Any]:
    reference = sample.get("reference") or {}
    output_json = json.dumps(reference, ensure_ascii=False)
    return {
        "prompt": state_to_prompt(sample.get("state") or [], max_chars),
        "output_tokens": max(MIN_OUTPUT_TOKENS, len(output_json) // CHARS_PER_TOKEN),
        "session_id": sample.get("session_id"),
        "sample_index": sample.get("sample_index"),
    }


def _percentile(values: list[int], q: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    return ordered[max(0, min(len(ordered) - 1, round(q * (len(ordered) - 1))))]


def build(input_path: Path, output_path: Path, max_chars: int = 0) -> dict[str, Any]:
    """导出压测数据集；按 session 分组、组内按 sample_index 递增排列。"""
    samples = load_tool_call_samples(input_path)
    samples.sort(key=lambda s: (s.get("session_id") or "", s.get("sample_index") or 0))

    output_path.parent.mkdir(parents=True, exist_ok=True)
    prompt_chars: list[int] = []
    output_tokens: list[int] = []
    with output_path.open("w", encoding="utf-8") as fh:
        for sample in samples:
            record = to_custom_record(sample, max_chars)
            prompt_chars.append(len(record["prompt"]))
            output_tokens.append(record["output_tokens"])
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    return {
        "records": len(samples),
        "sessions": len({s.get("session_id") for s in samples}),
        "prompt_chars_p50": _percentile(prompt_chars, 0.5),
        "prompt_chars_p90": _percentile(prompt_chars, 0.9),
        "prompt_chars_max": max(prompt_chars, default=0),
        "output_tokens_p50": _percentile(output_tokens, 0.5),
        "max_chars_cap": max_chars,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="MyCode 真实 agent 流量 → vLLM 压测数据集")
    parser.add_argument("--input", default=DEFAULT_INPUT)
    parser.add_argument("--out", default=DEFAULT_OUTPUT)
    parser.add_argument("--max-chars", type=int, default=0, help="prompt 字符上限（0=不限，从头部截断）")
    args = parser.parse_args(argv)

    input_path = Path(args.input)
    if not input_path.is_file():
        raise SystemExit(f"输入不存在: {input_path}（先跑 .venv/bin/python -m rl.export_sessions）")

    summary = build(input_path, Path(args.out), args.max_chars)
    print(f"[infra.build_bench_dataset] out={args.out}")
    for key, value in summary.items():
        print(f"  {key}={value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
