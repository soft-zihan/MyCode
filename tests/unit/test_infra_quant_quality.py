"""FP8 质量筛查工具：贪心解码一致性的采集与比较。"""

from __future__ import annotations

import json
from pathlib import Path

from infra.quant_quality import build_payload, compare, first_divergence, load_prompts, main


def _write_jsonl(path: Path, rows: list[dict]) -> Path:
    path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8"
    )
    return path


def _gen(index: int, text: str, session: str = "s1") -> dict:
    return {
        "index": index,
        "session_id": session,
        "text": text,
        "finish_reason": "stop",
        "prompt_tokens": 100,
        "completion_tokens": len(text),
        "elapsed_s": 0.1,
        "error": "",
    }


def test_first_divergence_identical_returns_minus_one() -> None:
    assert first_divergence("abc", "abc") == -1


def test_first_divergence_reports_index() -> None:
    assert first_divergence("abcdef", "abXdef") == 2


def test_first_divergence_prefix_returns_shorter_length() -> None:
    # 一个是另一个的前缀：分歧点就是短的那个结束的地方
    assert first_divergence("abc", "abcdef") == 3


def test_build_payload_is_greedy_and_deterministic() -> None:
    payload = build_payload("q2b", "hello", 128)
    assert payload["temperature"] == 0.0
    assert payload["seed"] == 1234
    assert payload["stream"] is False
    assert payload["max_tokens"] == 128
    assert payload["prompt"] == "hello"
    # 两次构造必须逐字节相同，否则「一致性」测的就不是量化而是采样
    assert build_payload("q2b", "hello", 128) == payload


def test_load_prompts_respects_limit(tmp_path: Path) -> None:
    ds = _write_jsonl(
        tmp_path / "ds.jsonl",
        [{"prompt": f"p{i}", "session_id": "s", "sample_index": i} for i in range(5)],
    )
    assert len(load_prompts(ds, 2)) == 2
    assert len(load_prompts(ds, 0)) == 5


def test_compare_all_identical(tmp_path: Path) -> None:
    a = _write_jsonl(tmp_path / "a.jsonl", [_gen(0, "same"), _gen(1, "same2")])
    b = _write_jsonl(tmp_path / "b.jsonl", [_gen(0, "same"), _gen(1, "same2")])
    out = tmp_path / "cmp.md"
    assert compare(_ns(a=a, b=b, out=str(out), show=10)) == 0
    text = out.read_text(encoding="utf-8")
    assert "2/2（100.0%）" in text
    assert "1.0000" in text


def test_compare_detects_divergence_and_positions_it(tmp_path: Path) -> None:
    a = _write_jsonl(tmp_path / "a.jsonl", [_gen(0, "aaaaBBBB")])
    b = _write_jsonl(tmp_path / "b.jsonl", [_gen(0, "aaaaXXXX")])
    out = tmp_path / "cmp.md"
    assert compare(_ns(a=a, b=b, out=str(out), show=10)) == 0
    text = out.read_text(encoding="utf-8")
    assert "0/1（0.0%）" in text
    # 发散点在第 4 个字符 = 输出的 50% 处
    assert "| 4（50%） |" in text


def test_compare_rejects_mismatched_counts(tmp_path: Path) -> None:
    a = _write_jsonl(tmp_path / "a.jsonl", [_gen(0, "x")])
    b = _write_jsonl(tmp_path / "b.jsonl", [_gen(0, "x"), _gen(1, "y")])
    assert compare(_ns(a=a, b=b, out="", show=10)) == 2


def test_compare_sorts_by_index_not_file_order(tmp_path: Path) -> None:
    # 采集是并发的，落盘顺序按 index 排过；比较必须再排一次，否则错位比对会假装一致
    a = _write_jsonl(tmp_path / "a.jsonl", [_gen(1, "one"), _gen(0, "zero")])
    b = _write_jsonl(tmp_path / "b.jsonl", [_gen(0, "zero"), _gen(1, "one")])
    out = tmp_path / "cmp.md"
    assert compare(_ns(a=a, b=b, out=str(out), show=10)) == 0
    assert "2/2（100.0%）" in out.read_text(encoding="utf-8")


def test_collect_fails_loudly_when_dataset_missing(tmp_path: Path, capsys) -> None:
    rc = main(
        [
            "collect",
            "--endpoint",
            "http://127.0.0.1:1/v1",
            "--model",
            "m",
            "--dataset",
            str(tmp_path / "nope.jsonl"),
            "--out",
            str(tmp_path / "o.jsonl"),
        ]
    )
    assert rc == 2
    assert "数据集不存在" in capsys.readouterr().err


class _NS:
    def __init__(self, **kw) -> None:
        self.__dict__.update(kw)


def _ns(**kw):
    return _NS(**kw)
