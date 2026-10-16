"""I3：从 Prometheus 把「GPU 利用率为什么上不去」的归因数字捞出来。

不手点面板：面板用来看形状，下结论用的数字必须能从命令行重跑、能落盘成产物，
否则文档里的数就没有可追溯来源（本轮就吃过这个亏：README 写了「每迭代 7.1 token」，
而落盘的产物里根本没有这一行）。

用法见同目录 README.md。
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Sequence
from pathlib import Path

# 别写 localhost：Windows 上 Python 的 urllib 会先试 ::1 并挂住，curl 则不会
DEFAULT_BASE = "http://127.0.0.1:9090/api/v1/query"

# {w} = 统计窗口。每条都是一次 instant query，用 *_over_time / increase 回看整个窗口。
QUERIES: tuple[tuple[str, str], ...] = (
    # --- GPU 侧（nvidia_smi_exporter）---
    ("GPU 利用率 峰值 %", "max_over_time(nvidia_gpu_utilization_percent[{w}])"),
    ("GPU 利用率 均值 %", "avg_over_time(nvidia_gpu_utilization_percent[{w}])"),
    ("GPU 显存带宽利用率 峰值 %", "max_over_time(nvidia_gpu_memory_utilization_percent[{w}])"),
    ("GPU 显存已用 峰值 MiB", "max_over_time(nvidia_gpu_memory_used_mib[{w}])"),
    ("GPU 功耗 峰值 W", "max_over_time(nvidia_gpu_power_draw_watts[{w}])"),
    ("GPU SM 频率 峰值 MHz", "max_over_time(nvidia_gpu_sm_clock_mhz[{w}])"),
    # --- 引擎侧：瓶颈在哪 ---
    ("vLLM KV cache 使用率 峰值 %", "max_over_time(vllm:kv_cache_usage_perc[{w}]) * 100"),
    ("并发 running 峰值", "max_over_time(vllm:num_requests_running[{w}])"),
    ("排队 waiting 峰值", "max_over_time(vllm:num_requests_waiting[{w}])"),
    ("抢占次数", "sum(increase(vllm:num_preemptions_total[{w}]))"),
    # --- 批饱满度：GPU 到底是「算不动」还是「没活干」，这一组才是答案 ---
    ("平均每迭代 token 数", "sum(increase(vllm:iteration_tokens_total_sum[{w}])) / clamp_min(sum(increase(vllm:iteration_tokens_total_count[{w}])), 1e-9)"),
    ("每迭代 token 数 p50", "histogram_quantile(0.5, sum by (le) (increase(vllm:iteration_tokens_total_bucket[{w}])))"),
    ("每迭代 token 数 p90", "histogram_quantile(0.9, sum by (le) (increase(vllm:iteration_tokens_total_bucket[{w}])))"),
    ("引擎迭代次数", "sum(increase(vllm:iteration_tokens_total_count[{w}]))"),
    # --- 缓存与吞吐 ---
    ("prefix 命中率（token 级）", "sum(increase(vllm:prompt_tokens_cached_total[{w}])) / sum(increase(vllm:prompt_tokens_total[{w}]))"),
    ("输入吞吐 峰值 tok/s", "max_over_time(rate(vllm:prompt_tokens_total[1m])[{w}:15s])"),
    ("输出吞吐 峰值 tok/s", "max_over_time(rate(vllm:generation_tokens_total[1m])[{w}:15s])"),
    ("TTFT p99 s", "histogram_quantile(0.99, sum by (le) (rate(vllm:time_to_first_token_seconds_bucket[{w}])))"),
    ("TTFT p50 s", "histogram_quantile(0.50, sum by (le) (rate(vllm:time_to_first_token_seconds_bucket[{w}])))"),
    # --- 时间花在哪 ---
    ("平均 prefill 耗时 s", "sum(increase(vllm:request_prefill_time_seconds_sum[{w}])) / sum(increase(vllm:request_prefill_time_seconds_count[{w}]))"),
    ("平均 decode 耗时 s", "sum(increase(vllm:request_decode_time_seconds_sum[{w}])) / sum(increase(vllm:request_decode_time_seconds_count[{w}]))"),
    ("平均排队耗时 s", "sum(increase(vllm:request_queue_time_seconds_sum[{w}])) / sum(increase(vllm:request_queue_time_seconds_count[{w}]))"),
    ("完成请求数", "sum(increase(vllm:request_success_total[{w}]))"),
    # --- 算术强度：本机这两个计数器恒为 0（见 README 诚实边界③），留着是为了让「拿不到」也可见 ---
    ("算术强度 FLOP/byte（总）", "sum(increase(vllm:estimated_flops_per_gpu_total[{w}])) / sum(increase(vllm:estimated_read_bytes_per_gpu_total[{w}]))"),
    ("累计读取 GB", "sum(increase(vllm:estimated_read_bytes_per_gpu_total[{w}])) / 1e9"),
    ("累计算力 TFLOP", "sum(increase(vllm:estimated_flops_per_gpu_total[{w}])) / 1e12"),
)


def build_queries(window: str) -> tuple[tuple[str, str], ...]:
    return tuple((label, expr.replace("{w}", window)) for label, expr in QUERIES)


def query_one(
    expr: str,
    base: str = DEFAULT_BASE,
    opener: Callable[..., object] = urllib.request.urlopen,
    at: float | None = None,
) -> str:
    params: dict[str, object] = {"query": expr}
    if at is not None:
        params["time"] = f"{at:.3f}"
    url = base + "?" + urllib.parse.urlencode(params)
    try:
        with opener(url, timeout=15) as resp:  # type: ignore[operator]
            data = json.loads(resp.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, ValueError) as exc:
        return f"<查询失败 {exc}>"
    result = data.get("data", {}).get("result", [])
    if not result:
        return "<空>"
    value = result[0].get("value", [None, "NaN"])[1]
    try:
        return f"{float(value):.4g}"
    except (TypeError, ValueError):
        return str(value)


def format_table(rows: Sequence[tuple[str, str]]) -> str:
    width = max((len(label) for label, _ in rows), default=0)
    lines = ["| 指标 | 值 |", "|---|---|"]
    lines += [f"| {label.ljust(width)} | {value} |" for label, value in rows]
    return "\n".join(lines) + "\n"


def collect(window: str, base: str = DEFAULT_BASE, at: float | None = None) -> tuple[tuple[str, str], ...]:
    return tuple((label, query_one(expr, base, at=at)) for label, expr in build_queries(window))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--window", default="15m", help="回看窗口，要盖住整轮压测（默认 15m）")
    parser.add_argument("--base", default=DEFAULT_BASE, help="Prometheus query API")
    parser.add_argument(
        "--at",
        type=float,
        default=None,
        help="评估时刻的 unix 秒；不给就是现在。用来把窗口对齐到某一档压测，别把相邻两档混在一起",
    )
    parser.add_argument("--out", default="", help="落盘路径；不给就打到 stdout")
    args = parser.parse_args(argv)

    table = format_table(collect(args.window, args.base, args.at))
    if args.out:
        # 必须显式 utf-8：Windows 侧重定向的默认编码会把中文写成 GBK，产物变乱码
        Path(args.out).write_text(table, encoding="utf-8")
        print(f"written: {args.out}")
    else:
        sys.stdout.write(table)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
