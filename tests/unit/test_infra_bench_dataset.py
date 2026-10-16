"""压测数据集构造：真实 agent 流量 → ShareGPT，且保持 session 分组顺序。"""

from __future__ import annotations

import json
from pathlib import Path

from infra.build_bench_dataset import build, load_tool_call_samples, state_to_prompt


def _sample(session_id: str, index: int, state: list[dict], name: str = "read_file") -> dict:
    return {
        "kind": "tool_call",
        "session_id": session_id,
        "sample_index": index,
        "state": state,
        "state_dropped_messages": 0,
        "reference": {"name": name, "arguments": json.dumps({"path": f"f{index}.py"})},
        "outcome": "success",
    }


def _write_samples(path: Path, records: list) -> Path:
    lines = [r if isinstance(r, str) else json.dumps(r, ensure_ascii=False) for r in records]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_load_skips_bad_lines_and_non_tool_call(tmp_path):
    path = _write_samples(tmp_path / "samples.jsonl", [
        {"kind": "trajectory", "session_id": "s1", "messages": []},
        _sample("s1", 0, [{"role": "user", "content": "hi"}]),
        '{"kind": "tool_call", "trunc',
    ])

    samples = load_tool_call_samples(path)

    assert len(samples) == 1
    assert samples[0]["session_id"] == "s1"


def test_state_to_prompt_keeps_tool_calls_and_truncates_from_head():
    state = [
        {"role": "user", "content": "OLD" * 100},
        {"role": "assistant", "content": "",
         "tool_calls": [{"id": "c1", "function": {"name": "grep_search", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "RECENT-RESULT"},
    ]

    full = state_to_prompt(state)
    capped = state_to_prompt(state, max_chars=40)

    assert "<|user|>" in full and "<|tool|>" in full
    assert "grep_search" in full and "RECENT-RESULT" in full
    # 从头部截断 → 保留最近的内容
    assert len(capped) == 40
    assert "RECENT-RESULT" in capped
    assert "OLD" not in capped


def test_build_groups_by_session_and_orders_by_index(tmp_path):
    src = _write_samples(tmp_path / "samples.jsonl", [
        _sample("s2", 1, [{"role": "user", "content": "b"}], name="edit_file"),
        _sample("s1", 5, [{"role": "user", "content": "a5"}], name="run_shell"),
        _sample("s1", 2, [{"role": "user", "content": "a2"}], name="list_files"),
    ])
    out = tmp_path / "bench.jsonl"

    summary = build(src, out)

    records = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    # 同 session 相邻、组内 sample_index 递增——压测回放顺序贴近真实，缓存命中才有意义
    assert [(r["session_id"], r["sample_index"]) for r in records] == [("s1", 2), ("s1", 5), ("s2", 1)]
    assert summary["records"] == 3 and summary["sessions"] == 2


def test_build_emits_custom_record_shape(tmp_path):
    src = _write_samples(tmp_path / "samples.jsonl", [
        _sample("s1", 0, [{"role": "user", "content": "读一下配置"}], name="read_file"),
    ])
    out = tmp_path / "bench.jsonl"

    build(src, out)

    record = json.loads(out.read_text(encoding="utf-8").strip())
    # vLLM CustomDataset 只认 prompt / output_tokens（其余列会被 pandas 保留但忽略）
    assert set(record) == {"prompt", "output_tokens", "session_id", "sample_index"}
    assert "读一下配置" in record["prompt"]
    assert record["output_tokens"] >= 16  # 有下限，避免 0 token 请求
    assert (record["session_id"], record["sample_index"]) == ("s1", 0)


def test_build_truncates_prompt_from_head_when_capped(tmp_path):
    long_state = [
        {"role": "user", "content": "HEADMARKER" + "x" * 500},
        {"role": "user", "content": "TAIL"},
    ]
    src = _write_samples(tmp_path / "samples.jsonl", [_sample("s1", 0, long_state)])
    out = tmp_path / "bench.jsonl"

    build(src, out, max_chars=60)

    record = json.loads(out.read_text(encoding="utf-8").strip())
    assert len(record["prompt"]) == 60
    assert "TAIL" in record["prompt"]
    assert "HEADMARKER" not in record["prompt"]  # 从头部丢，保留最近的上下文
