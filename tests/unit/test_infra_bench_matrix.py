"""压测矩阵驱动：命令构造、指标容错解析、汇总表、缺数据集时的早失败。"""

from __future__ import annotations

import json
import subprocess
import time

import pytest

from infra import bench_matrix as bm


def test_server_argv_reflects_switches():
    argv = bm.build_server_argv(bm.BenchConfig(
        name="x", model="Qwen/Qwen3.5-0.8B", prefix_caching=False,
        gpu_memory_utilization=0.7, max_num_batched_tokens=2048, quantization="fp8",
    ))

    joined = " ".join(argv)
    assert "--no-enable-prefix-caching" in joined
    assert "--enable-prefix-caching" not in joined.replace("--no-enable-prefix-caching", "")
    assert "--gpu-memory-utilization 0.7" in joined
    assert "--max-num-batched-tokens 2048" in joined
    assert "--quantization fp8" in joined
    # Qwen3.5 小模型是原生多模态：纯文本压测必须关掉多模态槽位
    assert "--limit-mm-per-prompt" in argv


def test_server_argv_chunked_prefill_tri_state():
    on = " ".join(bm.build_server_argv(bm.BenchConfig(name="a", chunked_prefill=True)))
    off = " ".join(bm.build_server_argv(bm.BenchConfig(name="b", chunked_prefill=False)))
    default = " ".join(bm.build_server_argv(bm.BenchConfig(name="c")))

    assert "--enable-chunked-prefill" in on
    assert "--no-enable-chunked-prefill" in off
    assert "chunked-prefill" not in default  # None = 交给引擎默认，不显式传


def test_bench_argv_custom_vs_random(tmp_path):
    cfg = bm.BenchConfig(name="s", dataset="custom", num_prompts=60)
    argv = bm.build_bench_argv(cfg, concurrency=4, result_path=tmp_path / "s_c4.json")
    joined = " ".join(argv)

    assert "--dataset-name custom" in joined
    assert str(bm.BENCH_DATASET) in joined
    assert "--max-concurrency 4" in joined
    assert "--result-filename s_c4.json" in joined
    assert "--save-result" in argv
    assert "--custom-output-len 128" in joined
    # 打乱会破坏「同会话相邻」的前缀复用，prefix caching 的对比就失真了 → 必须显式保序
    assert "--disable-shuffle" in argv

    cfg_random = bm.BenchConfig(name="r", dataset="random", random_prefix_len=0)
    random_argv = bm.build_bench_argv(cfg_random, 1, tmp_path / "r.json")
    joined_random = " ".join(random_argv)
    assert "--dataset-name random" in joined_random
    assert "--random-prefix-len 0" in joined_random
    assert "--disable-shuffle" in random_argv


def test_extract_metrics_tolerates_key_naming():
    new_style = {"ttft_mean_ms": 12.3456, "tpot_mean_ms": 3.1, "request_throughput": 2.5,
                 "completed": 60, "p99_ttft_ms": 40.0}
    old_style = {"mean_ttft_ms": 20.0, "mean_tpot_ms": 4.0, "output_throughput": 100.0}

    a = bm.extract_metrics(new_style)
    b = bm.extract_metrics(old_style)

    assert a["ttft_mean_ms"] == 12.346 and a["ttft_p99_ms"] == 40.0
    assert a["req_throughput"] == 2.5 and a["completed"] == 60
    assert b["ttft_mean_ms"] == 20.0 and b["out_tok_throughput"] == 100.0
    assert b["ttft_p99_ms"] is None  # 缺失指标是 None，不是 0（0 会被误读成极快）


def test_to_markdown_renders_header_and_rows():
    md = bm.to_markdown([{"config": "base_pc_on", "concurrency": 4, "ttft_mean_ms": 12.3}])

    assert md.startswith("| config | concurrency |")
    assert "| base_pc_on | 4 |" in md
    assert bm.to_markdown([]) == "（无结果）\n"


def test_main_fails_early_when_real_traffic_dataset_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(bm, "BENCH_DATASET", tmp_path / "missing.jsonl")

    with pytest.raises(SystemExit) as exc:
        bm.main(["--only", "base_pc_on", "--run-dir", str(tmp_path / "run")])

    assert "build_bench_dataset" in str(exc.value)


def test_dry_run_prints_commands_without_touching_gpu(tmp_path, monkeypatch, capsys):
    dataset = tmp_path / "bench.jsonl"
    dataset.write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(bm, "BENCH_DATASET", dataset)
    monkeypatch.setattr(bm, "CONFIGS", (bm.BenchConfig(name="only_one", concurrencies=(1,)),))

    rc = bm.main(["--dry-run", "--run-dir", str(tmp_path / "run")])

    out = capsys.readouterr().out
    assert rc == 0
    assert "vllm serve" in out and "vllm bench serve" in out
    assert not (tmp_path / "run" / "matrix.md").exists()  # dry-run 不产结果文件


def test_configs_have_unique_names():
    names = [c.name for c in bm.CONFIGS]
    assert len(names) == len(set(names))
    assert json.dumps([c.__dict__ for c in bm.CONFIGS], default=str)  # 配置必须可序列化落盘


METRICS_COUNTERS = """# HELP vllm:prefix_cache_queries Number of prefix cache queries.
# TYPE vllm:prefix_cache_queries counter
vllm:prefix_cache_queries_total{engine="0",model_name="bench"} 1000.0
vllm:prefix_cache_hits_total{engine="0",model_name="bench"} 900.0
vllm:connector_prefix_cache_queries_total{engine="0"} 777.0
vllm:connector_prefix_cache_hits_total{engine="0"} 777.0
vllm:num_requests_running{model_name="bench"} 0.0
"""


def test_parse_prefix_cache_normalizes_names_and_skips_connector():
    parsed = bm.parse_prefix_cache(METRICS_COUNTERS)

    # connector_* 是二级连接器缓存，混进来会把命中率算错
    assert parsed == {"queries": 1000.0, "hits": 900.0}


def test_parse_prefix_cache_accepts_legacy_gpu_prefixed_names():
    text = ('vllm:gpu_prefix_cache_queries_total{a="b"} 200.0\n'
            'vllm:gpu_prefix_cache_hits_total{a="b"} 100.0\n')

    assert bm.parse_prefix_cache(text) == {"queries": 200.0, "hits": 100.0}


def test_prefix_hit_rate_uses_delta_not_absolute():
    before = {"queries": 1000.0, "hits": 500.0}
    after = {"queries": 2000.0, "hits": 1460.0}

    # 绝对命中率是 0.73，本轮压测的增量命中率是 (1460-500)/(2000-1000)=0.96
    assert bm.prefix_hit_rate(before, after) == 0.96


def test_prefix_hit_rate_falls_back_to_gauge_then_none():
    assert bm.prefix_hit_rate({}, {"hit_rate": 0.9662}) == 0.9662
    assert bm.prefix_hit_rate({}, {}) is None


def test_scrape_metrics_tolerates_unreachable_endpoint():
    assert bm.scrape_metrics("http://127.0.0.1:1", timeout=1.0) == ""


def test_wait_for_health_returns_fast_when_server_process_dies():
    dead = subprocess.Popen(["false"])

    t0 = time.time()
    ok = bm.wait_for_health(timeout_s=120, poll_s=0.2, proc=dead)
    elapsed = time.time() - t0

    assert ok is False
    # 关键回归：服务进程已退出时不能傻等满 timeout（曾经白等 900s）
    assert elapsed < 15


def test_last_error_picks_last_error_line_and_tolerates_missing_file(tmp_path):
    log = tmp_path / "server.log"
    log.write_text("INFO starting\nERROR 123 EngineCore failed to start\nINFO bye\n", encoding="utf-8")

    assert "EngineCore failed" in bm.last_error(log)
    assert bm.last_error(tmp_path / "missing.log") == ""


def test_peak_kv_cache_usage_takes_max_and_tolerates_no_data(tmp_path):
    log = tmp_path / "server.log"
    log.write_text(
        "INFO Engine 000: Avg prompt throughput: 169.0 tokens/s, GPU KV cache usage: 12.5%\n"
        "INFO Engine 000: Avg prompt throughput: 647.8 tokens/s, GPU KV cache usage: 63.2%\n"
        "INFO Engine 000: Avg prompt throughput: 0.0 tokens/s, GPU KV cache usage: 40.0%\n",
        encoding="utf-8")

    assert bm.peak_kv_cache_usage(log) == 63.2
    assert bm.peak_kv_cache_usage(tmp_path / "missing.log") is None
    empty = tmp_path / "empty.log"
    empty.write_text("INFO no kv lines here\n", encoding="utf-8")
    assert bm.peak_kv_cache_usage(empty) is None


def test_peak_kv_cache_usage_respects_offset(tmp_path):
    log = tmp_path / "server.log"
    log.write_text(
        "INFO Engine 000: Avg prompt throughput: 100.0 tokens/s, GPU KV cache usage: 90.0%\n"
        "INFO Engine 000: Avg prompt throughput: 100.0 tokens/s, GPU KV cache usage: 20.0%\n",
        encoding="utf-8")
    first_line_len = len(log.read_bytes().splitlines(keepends=True)[0])

    # 不截偏移 = 整份日志的峰值（会把上一轮压测的数字算进本轮）
    assert bm.peak_kv_cache_usage(log) == 90.0
    assert bm.peak_kv_cache_usage(log, first_line_len) == 20.0


def test_warmup_enabled_by_default_so_concurrency_levels_are_comparable():
    # 三档并发共用一个服务；不预热会让第一档冷缓存、后两档热缓存，对比失真
    assert bm.BenchConfig(name="x").warmup is True
