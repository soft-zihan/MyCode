"""vLLM 推理优化开关矩阵压测驱动（I1 基线 + I2 开关对比）。

一次运行 = 起一个 vllm serve（给定开关）→ 等健康 → 按并发梯度跑 vllm bench serve
→ 收 JSON 结果 → 关服务；多组配置串行跑完汇总成 markdown 表。

设计取舍：
- **每个配置独立起服务**——prefix caching / chunked prefill / 量化都是启动期参数，
  不能热切；串行是为了 8GB 单卡上不被显存争用污染结果。
- 结果只取相对差：4060 Laptop 8GB 的绝对吞吐没有对外意义（见 mindmaps/28 §6）。
- 负载两种：``custom``（MyCode 真实 agent 流量，由 build_bench_dataset.py 生成；不用
  sharegpt 是因为它有 max_prompt_len=1024 的硬过滤，会**静默丢掉**真实长 prompt）
  与 ``random``（可控前缀长度，用于隔离 prefix caching 的贡献）。

用法（WSL 内，infra venv）::

    ~/venvs/infra/bin/python -m infra.bench_matrix --dry-run          # 只打印命令
    ~/venvs/infra/bin/python -m infra.bench_matrix --only base_pc_off,base_pc_on
    ~/venvs/infra/bin/python -m infra.bench_matrix                    # 跑全部

产物落 ``data/infra/<run-id>/``：每组配置的 bench JSON + 汇总 matrix.md / matrix.jsonl。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data" / "infra"
BENCH_DATASET = DATA_DIR / "bench_custom.jsonl"


def _vllm_bin() -> str:
    """用当前解释器同 venv 里的 vllm——否则依赖 PATH，换 venv 跑就会静默用到别的引擎。"""
    candidate = Path(sys.executable).parent / "vllm"
    return str(candidate) if candidate.exists() else "vllm"


VLLM_BIN = _vllm_bin()

DEFAULT_MODEL_NAME = "Qwen3.5-0.8B"
MODEL_2B_NAME = "Qwen3.5-2B"
MODEL_DIR = Path(os.environ.get("INFRA_MODEL_DIR", str(Path.home() / "models")))
HF_MIRROR = "https://hf-mirror.com"
PORT = 8100
BASE_URL = f"http://127.0.0.1:{PORT}"


def model_ref(name: str) -> str:
    """本地已下载就用本地路径（离线可复现），否则回落 HF 仓库 id。"""
    local = MODEL_DIR / name
    return str(local) if local.is_dir() else f"Qwen/{name}"


DEFAULT_MODEL = model_ref(DEFAULT_MODEL_NAME)

# 指标键在不同 vllm 版本间有差异，这里按优先级容错取
METRIC_KEYS = {
    "ttft_mean_ms": ("ttft_mean_ms", "mean_ttft_ms", "avg_ttft_ms"),
    "ttft_p99_ms": ("ttft_p99_ms", "p99_ttft_ms"),
    "tpot_mean_ms": ("tpot_mean_ms", "mean_tpot_ms", "avg_tpot_ms"),
    "itl_mean_ms": ("itl_mean_ms", "mean_itl_ms", "avg_itl_ms"),
    "e2el_mean_ms": ("e2el_mean_ms", "mean_e2el_ms", "avg_e2el_ms"),
    "req_throughput": ("request_throughput", "request_throughput_per_s"),
    "out_tok_throughput": ("output_throughput", "output_throughput_per_s"),
    "total_tok_throughput": ("total_token_throughput",),
    "completed": ("completed", "completed_prompts"),
}


@dataclass
class BenchConfig:
    name: str
    model: str = DEFAULT_MODEL
    max_model_len: int = 16384  # 真实 agent prompt 最长约 4-6K token，8192 会被顶穿
    gpu_memory_utilization: float = 0.85
    prefix_caching: bool = True
    chunked_prefill: bool | None = None  # None=不显式传，用引擎默认
    max_num_batched_tokens: int | None = None
    quantization: str | None = None
    extra_server_args: list[str] = field(default_factory=list)
    dataset: str = "custom"
    custom_output_len: int = 128
    num_prompts: int = 60
    concurrencies: tuple[int, ...] = (1, 4, 16)
    warmup: bool = True
    random_input_len: int = 2048
    random_output_len: int = 128
    random_prefix_len: int = 1024


# I1 基线 + I2 开关矩阵。命名即结论维度，便于表里对照。
CONFIGS: tuple[BenchConfig, ...] = (
    BenchConfig(name="base_pc_off", prefix_caching=False),
    BenchConfig(name="base_pc_on", prefix_caching=True),
    BenchConfig(name="gpu_util_070", prefix_caching=True, gpu_memory_utilization=0.70),
    BenchConfig(name="max_tok_2048", prefix_caching=True, max_num_batched_tokens=2048),
    # 注：没有「关 chunked prefill」这一组——Qwen3.5 是混合架构，vLLM 直接拒绝启动：
    # "Chunked prefill is required for mamba cache mode 'align'"。该开关在此模型上不可关。
    BenchConfig(name="random_prefix", prefix_caching=True, dataset="random", num_prompts=48),
    BenchConfig(name="random_no_prefix", prefix_caching=True, dataset="random",
                num_prompts=48, random_prefix_len=0),
    # 量化对照：同一个 2B 模型，BF16 基线 vs 在线 FP8（Ada/cc8.9 原生支持 FP8）。
    # 只比显存与吞吐，**不比质量**——质量退化要另做任务级评测，别在这里下结论。
    # max-num-seqs 必须两边一致才公平：Qwen3.5 是混合架构，每条并发解码序列要占一个
    # Mamba cache block；BF16 的 2B 权重在 8GB 上只剩 230 块 < 默认 256，服务直接起不来
    # （"max_num_seqs (256) exceeds available Mamba cache blocks (230)"）。
    BenchConfig(name="bf16_2b", model=model_ref(MODEL_2B_NAME), prefix_caching=True,
                extra_server_args=["--max-num-seqs", "128"]),
    BenchConfig(name="fp8_2b", model=model_ref(MODEL_2B_NAME), prefix_caching=True,
                quantization="fp8", extra_server_args=["--max-num-seqs", "128"]),
)


def build_server_argv(cfg: BenchConfig) -> list[str]:
    argv = [
        VLLM_BIN, "serve", cfg.model,
        "--served-model-name", "bench",
        "--port", str(PORT),
        "--max-model-len", str(cfg.max_model_len),
        "--gpu-memory-utilization", str(cfg.gpu_memory_utilization),
        "--enable-prefix-caching" if cfg.prefix_caching else "--no-enable-prefix-caching",
    ]
    if cfg.chunked_prefill is True:
        argv.append("--enable-chunked-prefill")
    elif cfg.chunked_prefill is False:
        argv.append("--no-enable-chunked-prefill")
    if cfg.max_num_batched_tokens:
        argv += ["--max-num-batched-tokens", str(cfg.max_num_batched_tokens)]
    if cfg.quantization:
        argv += ["--quantization", cfg.quantization]
    # Qwen3.5 小模型是原生多模态；纯文本压测把多模态槽位关掉，省显存也少一个变量
    argv += ["--limit-mm-per-prompt", '{"image":0,"video":0}']
    argv += cfg.extra_server_args
    return argv


def build_bench_argv(cfg: BenchConfig, concurrency: int, result_path: Path) -> list[str]:
    argv = [
        VLLM_BIN, "bench", "serve",
        "--backend", "vllm",
        "--base-url", BASE_URL,
        "--model", cfg.model,
        "--served-model-name", "bench",
        "--num-prompts", str(cfg.num_prompts),
        "--max-concurrency", str(concurrency),
        "--save-result",
        "--result-dir", str(result_path.parent),
        "--result-filename", result_path.name,
        # 默认只报 ttft/tpot/itl，不显式要 e2el 就拿不到端到端延迟
        "--percentile-metrics", "ttft,tpot,itl,e2el",
    ]
    # 顺序必须固定：打乱会破坏「同会话相邻」的前缀复用，prefix caching 的对比就失真了
    argv.append("--disable-shuffle")
    if cfg.dataset == "custom":
        argv += ["--dataset-name", "custom", "--dataset-path", str(BENCH_DATASET),
                 "--custom-output-len", str(cfg.custom_output_len)]
    else:
        argv += ["--dataset-name", "random",
                 "--random-input-len", str(cfg.random_input_len),
                 "--random-output-len", str(cfg.random_output_len),
                 "--random-prefix-len", str(cfg.random_prefix_len)]
    return argv


def extract_metrics(result: dict[str, Any]) -> dict[str, Any]:
    """从 bench 结果 JSON 里按容错优先级取指标（版本间键名有差异）。"""
    out: dict[str, Any] = {}
    for canonical, candidates in METRIC_KEYS.items():
        for key in candidates:
            if result.get(key) is not None:
                value = result[key]
                out[canonical] = round(value, 3) if isinstance(value, float) else value
                break
        out.setdefault(canonical, None)
    return out


def to_markdown(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "（无结果）\n"
    columns = ["config", "concurrency", "completed", "ttft_mean_ms", "ttft_p99_ms",
               "tpot_mean_ms", "itl_mean_ms", "e2el_mean_ms",
               "req_throughput", "out_tok_throughput", "total_tok_throughput",
               "engine_prefix_hit_rate", "peak_kv_cache_pct", "peak_vram_mb", "bench_s"]
    lines = ["| " + " | ".join(columns) + " |",
             "|" + "|".join(["---"] * len(columns)) + "|"]
    for row in rows:
        lines.append("| " + " | ".join(str(row.get(c, "")) for c in columns) + " |")
    return "\n".join(lines) + "\n"


def scrape_metrics(base_url: str = BASE_URL, timeout: float = 10.0) -> str:
    """抓 vLLM 的 Prometheus /metrics 原文（失败返回空串，不让压测因此中断）。"""
    try:
        with urllib.request.urlopen(f"{base_url}/metrics", timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, ConnectionError, TimeoutError, OSError):
        return ""


# vLLM 各版本 prefix cache 指标命名不一（vllm:prefix_cache_* / vllm:gpu_prefix_cache_*，
# counter 还会带 _total 后缀），归一到 canonical 键；connector_* 是二级连接器缓存，要排除。
PREFIX_CACHE_ALIASES = {
    "queries": ("prefix_cache_queries", "gpu_prefix_cache_queries"),
    "hits": ("prefix_cache_hits", "gpu_prefix_cache_hits"),
    "hit_rate": ("prefix_cache_hit_rate", "gpu_prefix_cache_hit_rate"),
}


def parse_prefix_cache(metrics_text: str) -> dict[str, float]:
    """从 /metrics 文本里取 prefix cache 指标，归一成 queries / hits / hit_rate。"""
    out: dict[str, float] = {}
    for line in metrics_text.splitlines():
        if line.startswith("#") or "prefix_cache" not in line:
            continue
        head, _, rest = line.partition(" ")
        name = head.split("{", 1)[0]  # 剥掉 Prometheus 标签，否则永远匹配不上
        if "connector" in name:
            continue
        base = name.split(":", 1)[-1].removesuffix("_total")
        try:
            value = float(rest.strip())
        except ValueError:
            continue
        for canonical, aliases in PREFIX_CACHE_ALIASES.items():
            if base in aliases:
                out[canonical] = value
    return out


def prefix_hit_rate(before: dict[str, float], after: dict[str, float]) -> float | None:
    """引擎侧 prefix cache 命中率 = Δhits / Δqueries；无计数时退回 gauge，都没有则 None。

    这是与 README「>96% 命中率」对账的引擎侧口径——README 那个数来自网关返回的
    cached_tokens（MyCode stats 事件），两者应当同向；对不上就说明有一侧在骗人。
    """
    queries = after.get("queries", 0) - before.get("queries", 0)
    hits = after.get("hits", 0) - before.get("hits", 0)
    if queries > 0:
        return round(hits / queries, 4)
    if after.get("hit_rate") is not None:
        return round(after["hit_rate"], 4)
    return None


def wait_for_health(timeout_s: float = 900.0, poll_s: float = 5.0,
                    proc: subprocess.Popen | None = None) -> bool:
    """轮询 /health 直到就绪；模型首次加载 + 图编译可能要几分钟。

    传了 proc 时，进程一退出就立刻返回 False——否则服务启动失败会白等满 timeout。
    """
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if proc is not None and proc.poll() is not None:
            return False
        try:
            with urllib.request.urlopen(f"{BASE_URL}/health", timeout=5) as resp:
                if resp.status == 200:
                    return True
        except (urllib.error.URLError, ConnectionError, TimeoutError, OSError):
            pass
        time.sleep(poll_s)
    return False


def last_error(log_path: Path, max_chars: int = 300) -> str:
    """从服务日志里捞最后一条错误行，供失败时直接定位（省一次翻日志）。"""
    try:
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    for line in reversed(lines):
        if "error" in line.lower() or "Traceback" in line:
            return line.strip()[:max_chars]
    return ""


def run_config(cfg: BenchConfig, run_dir: Path, dry_run: bool = False) -> list[dict[str, Any]]:
    server_argv = build_server_argv(cfg)
    rows: list[dict[str, Any]] = []
    run_dir.mkdir(parents=True, exist_ok=True)

    if dry_run:
        print(f"# [{cfg.name}] server: {' '.join(shlex.quote(a) for a in server_argv)}")
        for concurrency in cfg.concurrencies:
            result_path = run_dir / f"{cfg.name}_c{concurrency}.json"
            bench_argv = build_bench_argv(cfg, concurrency, result_path)
            print(f"# [{cfg.name}] bench c={concurrency}: {' '.join(shlex.quote(a) for a in bench_argv)}")
        return rows

    server_log = run_dir / f"{cfg.name}.server.log"
    with server_log.open("w", encoding="utf-8") as log:
        server = subprocess.Popen(server_argv, stdout=log, stderr=subprocess.STDOUT,
                                  cwd=str(PROJECT_ROOT), text=True)
        try:
            if not wait_for_health(proc=server):
                reason = (f"server exited rc={server.returncode}" if server.poll() is not None
                          else "server not healthy within timeout")
                rows.append({"config": cfg.name, "concurrency": "-",
                             "error": f"{reason}: {last_error(server_log)}",
                             "server_log": str(server_log)})
                return rows
            if cfg.warmup:
                # 先跑一遍不计入结果：三档并发共用一个服务，不预热的话第一档是冷缓存、
                # 后两档被前一轮焐热，prefix caching 的收益会虚高（run1 实测踩过）。
                warm_path = run_dir / f"{cfg.name}_warmup.json"
                subprocess.run(build_bench_argv(cfg, max(cfg.concurrencies), warm_path),
                               cwd=str(PROJECT_ROOT), capture_output=True, text=True)
            for concurrency in cfg.concurrencies:
                result_path = run_dir / f"{cfg.name}_c{concurrency}.json"
                bench_argv = build_bench_argv(cfg, concurrency, result_path)
                cache_before = parse_prefix_cache(scrape_metrics())
                log_offset = server_log.stat().st_size if server_log.exists() else 0
                t0 = time.time()
                with VramSampler() as sampler:
                    bench = subprocess.run(bench_argv, cwd=str(PROJECT_ROOT),
                                           capture_output=True, text=True)
                elapsed = round(time.time() - t0, 1)
                cache_after = parse_prefix_cache(scrape_metrics())
                bench_log = run_dir / f"{cfg.name}_c{concurrency}.bench.log"
                bench_log.write_text((bench.stdout or "") + "\n--stderr--\n" + (bench.stderr or ""),
                                     encoding="utf-8")
                row: dict[str, Any] = {"config": cfg.name, "concurrency": concurrency, "bench_s": elapsed,
                                       "engine_prefix_hit_rate": prefix_hit_rate(cache_before, cache_after),
                                       "peak_vram_mb": sampler.peak_mb}
                if result_path.is_file():
                    row.update(extract_metrics(json.loads(result_path.read_text(encoding="utf-8"))))
                    row["peak_kv_cache_pct"] = peak_kv_cache_usage(server_log, log_offset)
                else:
                    row["error"] = f"rc={bench.returncode} no result json (see {bench_log.name})"
                rows.append(row)
        finally:
            server.terminate()
            try:
                server.wait(timeout=60)
            except subprocess.TimeoutExpired:
                server.kill()
    return rows


KV_USAGE_RE = re.compile(r"GPU KV cache usage:\s*([0-9.]+)\s*%")


def peak_kv_cache_usage(server_log: Path, offset: int = 0) -> float | None:
    """服务日志里 GPU KV cache 使用率的峰值。

    offset 用来只看本轮压测新增的日志——服务日志跨该配置的所有压测轮次，
    不截偏移就会把整组的峰值填到每一行，列名与含义不符。
    """
    try:
        with server_log.open("rb") as fh:
            fh.seek(offset)
            text = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    values = [float(m) for m in KV_USAGE_RE.findall(text)]
    return round(max(values), 2) if values else None


class VramSampler:
    """压测期间轮询 nvidia-smi 取显存峰值。

    口径注意：WSL 下 nvidia-smi 报的是**整卡**已用显存（含其他进程），
    不是本进程独占；本机压测时无其他 GPU 负载，可近似当作服务占用。
    """

    def __init__(self, interval_s: float = 1.0) -> None:
        self.interval_s = interval_s
        self.peak_mb: int | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _poll(self) -> None:
        while not self._stop.is_set():
            try:
                out = subprocess.run(
                    ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                    capture_output=True, text=True, timeout=10)
                value = int(out.stdout.strip().splitlines()[0])
                self.peak_mb = value if self.peak_mb is None else max(self.peak_mb, value)
            except (OSError, ValueError, IndexError, subprocess.TimeoutExpired):
                pass
            self._stop.wait(self.interval_s)

    def __enter__(self) -> "VramSampler":
        self._thread = threading.Thread(target=self._poll, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="vLLM 推理优化开关矩阵压测")
    parser.add_argument("--only", default="", help="只跑指定配置名（逗号分隔）")
    parser.add_argument("--run-dir", default="", help="产物目录（默认 data/infra/<时间戳>）")
    parser.add_argument("--dry-run", action="store_true", help="只打印将执行的命令")
    parser.add_argument("--model", default="", help="覆盖所有配置的模型")
    parser.add_argument("--num-prompts", type=int, default=0,
                        help="覆盖所有配置的请求数（0=用配置默认；用来把单轮时长压进预算）")
    parser.add_argument("--concurrencies", default="",
                        help="覆盖并发梯度，逗号分隔，如 1,4,16")
    args = parser.parse_args(argv)

    run_dir = Path(args.run_dir) if args.run_dir else DATA_DIR / time.strftime("%Y%m%d_%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=True)

    selected = CONFIGS
    if args.only:
        wanted = {n.strip() for n in args.only.split(",") if n.strip()}
        selected = tuple(c for c in CONFIGS if c.name in wanted)
        if not selected:
            raise SystemExit(f"--only 未匹配任何配置：{sorted(wanted)}（可选 {[c.name for c in CONFIGS]}）")
    overrides: dict[str, Any] = {}
    if args.model:
        overrides["model"] = args.model
    if args.num_prompts:
        overrides["num_prompts"] = args.num_prompts
    if args.concurrencies:
        overrides["concurrencies"] = tuple(
            int(x) for x in args.concurrencies.split(",") if x.strip())
    if overrides:
        selected = tuple(BenchConfig(**{**c.__dict__, **overrides}) for c in selected)

    if any(c.dataset == "custom" for c in selected) and not BENCH_DATASET.is_file():
        raise SystemExit(
            f"缺少真实流量数据集 {BENCH_DATASET}\n"
            "先生成：.venv/bin/python -m infra.build_bench_dataset"
        )

    rows: list[dict[str, Any]] = []
    for cfg in selected:
        print(f"[bench_matrix] === {cfg.name} (model={cfg.model}, dataset={cfg.dataset}) ===", flush=True)
        rows.extend(run_config(cfg, run_dir, dry_run=args.dry_run))

    if not args.dry_run:
        (run_dir / "matrix.jsonl").write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")
        (run_dir / "matrix.md").write_text(to_markdown(rows), encoding="utf-8")
        print(f"[bench_matrix] run_dir={run_dir}")
        print(to_markdown(rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
