"""归因查询脚本：窗口展开、表格渲染、Prometheus 响应解析、UTF-8 落盘。"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

import infra.observability.query_attribution as qa


class _Resp(io.BytesIO):
    """urlopen 返回值的替身：支持 with 语义 + read()。"""

    def __enter__(self) -> "_Resp":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


def _opener(payload: dict, seen: list[str] | None = None):
    body = json.dumps(payload).encode("utf-8")

    def open(url: str, timeout: int = 0) -> _Resp:
        if seen is not None:
            seen.append(url)
        return _Resp(body)

    return open


def _instant(value: str) -> dict:
    return {"data": {"result": [{"metric": {}, "value": [1760000000.0, value]}]}}


def test_build_queries_expands_window_and_leaves_no_placeholder() -> None:
    rows = qa.build_queries("7m")
    assert len(rows) == len(qa.QUERIES)
    for _label, expr in rows:
        assert "{w}" not in expr
        assert "7m" in expr


def test_batch_fullness_queries_exist() -> None:
    """回归钉：README 的头号结论来自「每迭代 token 数」，可它曾经不在查询表里，
    于是文档写了一个没有任何产物能支撑的数字。"""
    labels = " ".join(label for label, _ in qa.QUERIES)
    exprs = " ".join(expr for _, expr in qa.QUERIES)
    assert "平均每迭代 token 数" in labels
    assert "iteration_tokens_total_sum" in exprs
    assert "iteration_tokens_total_bucket" in exprs


def test_format_table_renders_header_and_every_row() -> None:
    text = qa.format_table([("KV 峰值 %", "10.73"), ("抢占次数", "0")])
    lines = text.splitlines()
    assert lines[0] == "| 指标 | 值 |"
    assert lines[1] == "|---|---|"
    assert len(lines) == 4
    assert "| KV 峰值 % | 10.73 |" in text
    assert "| 抢占次数    | 0 |" in text


def test_format_table_handles_empty_rows() -> None:
    assert qa.format_table([]) == "| 指标 | 值 |\n|---|---|\n"


def test_query_one_parses_instant_vector() -> None:
    assert qa.query_one("up", opener=_opener(_instant("0.4265"))) == "0.4265"
    assert qa.query_one("up", opener=_opener(_instant("91.1500"))) == "91.15"


def test_query_one_reports_empty_result_set() -> None:
    assert qa.query_one("up", opener=_opener({"data": {"result": []}})) == "<空>"


def test_query_one_keeps_non_numeric_value_verbatim() -> None:
    assert qa.query_one("up", opener=_opener(_instant("N/A"))) == "N/A"


def test_query_one_survives_transport_failure() -> None:
    def boom(_url: str, timeout: int = 0) -> _Resp:
        raise OSError("connection refused")

    assert qa.query_one("up", opener=boom).startswith("<查询失败")


def test_query_one_pins_evaluation_time_when_given() -> None:
    """--at 存在的理由：把统计窗口对齐到单独一档压测，不让相邻两档混进同一个平均值。"""
    seen: list[str] = []
    qa.query_one("up", opener=_opener(_instant("1"), seen), at=1760000000.5)
    assert "time=1760000000.500" in seen[0]
    qa.query_one("up", opener=_opener(_instant("1"), seen))
    assert "time=" not in seen[1]


def test_main_writes_utf8_artifact(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """产物必须是 UTF-8：Windows 侧默认编码会把中文写成 GBK，文档就再也对不上产物。"""
    monkeypatch.setattr(qa, "collect", lambda window, base, at=None: (("GPU 利用率 峰值 %", "73"),))
    out = tmp_path / "attribution.md"
    assert qa.main(["--out", str(out)]) == 0
    assert out.read_bytes().decode("utf-8") == "| 指标 | 值 |\n|---|---|\n| GPU 利用率 峰值 % | 73 |\n"
