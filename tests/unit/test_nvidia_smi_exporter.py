"""nvidia-smi exporter：数值解析、Prometheus 文本渲染、采样缓存。"""

from __future__ import annotations

import infra.observability.nvidia_smi_exporter as exp


def _row(**overrides: str) -> list[str]:
    """按 FIELDS 顺序造一行 nvidia-smi 输出。"""
    defaults = {
        "utilization.gpu": "37",
        "utilization.memory": "12",
        "memory.used": "6343",
        "memory.total": "8188",
        "temperature.gpu": "61",
        "power.draw": "48.20",
        "clocks.sm": "2100",
        "fan.speed": "N/A",  # 笔记本卡常见：不支持就返回 N/A
    }
    defaults.update(overrides)
    return [defaults[field] for field, _, _ in exp.FIELDS]


def test_to_number_parses_ints_floats_and_rejects_placeholders() -> None:
    assert exp.to_number("45") == 45.0
    assert exp.to_number("48.20") == 48.20
    assert exp.to_number("N/A") is None
    assert exp.to_number("[Not Supported]") is None
    assert exp.to_number("") is None


def test_query_string_covers_every_declared_field() -> None:
    for field, _, _ in exp.FIELDS:
        assert field in exp.QUERY


def test_render_emits_help_and_type_for_every_field() -> None:
    text = exp.render([_row()])
    for _field, metric, kind in exp.FIELDS:
        assert f"# TYPE {metric} {kind}" in text


def test_render_skips_unsupported_values_instead_of_faking_zero() -> None:
    text = exp.render([_row(**{"fan.speed": "N/A"})])
    assert "nvidia_gpu_fan_speed_percent{" not in text
    # 其它字段照常导出
    assert 'nvidia_gpu_utilization_percent{gpu="0"' in text
    assert 'nvidia_gpu_memory_used_mib{gpu="0"' in text


def test_render_labels_multiple_gpus_by_index() -> None:
    text = exp.render([_row(), _row(**{"utilization.gpu": "99"})])
    assert 'nvidia_gpu_utilization_percent{gpu="0",field="utilization.gpu"} 37.0' in text
    assert 'nvidia_gpu_utilization_percent{gpu="1",field="utilization.gpu"} 99.0' in text


def test_sampler_caches_within_min_interval(monkeypatch) -> None:
    calls = {"n": 0}

    def fake_query(timeout_s: float = 10.0) -> list[list[str]]:
        calls["n"] += 1
        return [_row()]

    monkeypatch.setattr(exp, "query_nvidia_smi", fake_query)
    sampler = exp.Sampler(min_interval_s=60.0)
    first = sampler.body()
    second = sampler.body()
    assert first == second
    assert calls["n"] == 1  # 第二次走缓存，没有再 fork 一次 nvidia-smi


def test_sampler_refreshes_after_interval(monkeypatch) -> None:
    calls = {"n": 0}

    def fake_query(timeout_s: float = 10.0) -> list[list[str]]:
        calls["n"] += 1
        return [_row(**{"utilization.gpu": str(calls["n"])})]

    monkeypatch.setattr(exp, "query_nvidia_smi", fake_query)
    sampler = exp.Sampler(min_interval_s=0.0)
    sampler.body()
    sampler.body()
    assert calls["n"] == 2


def test_sampler_serves_stale_body_when_query_fails(monkeypatch) -> None:
    """采集中途 GPU 查询失败时，宁可给出上一次的旧数据，也不要让 scrape 直接 500。"""
    state = {"fail": False}

    def fake_query(timeout_s: float = 10.0) -> list[list[str]]:
        if state["fail"]:
            raise RuntimeError("nvidia-smi 挂了")
        return [_row()]

    monkeypatch.setattr(exp, "query_nvidia_smi", fake_query)
    sampler = exp.Sampler(min_interval_s=0.0)
    good = sampler.body()
    state["fail"] = True
    assert sampler.body() == good
    assert sampler.errors == 1


def test_query_raises_when_binary_missing(monkeypatch) -> None:
    monkeypatch.setattr(exp.shutil, "which", lambda _name: None)
    try:
        exp.query_nvidia_smi()
    except RuntimeError as exc:
        assert "nvidia-smi" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("找不到 nvidia-smi 时必须报错，不能静默返回空")
