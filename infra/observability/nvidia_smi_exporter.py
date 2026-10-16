"""nvidia-smi 的 Prometheus exporter（I3 的 GPU 指标来源）。

**为什么不用 DCGM exporter**（本来那是标准答案，也是简历关键词）：实测在 RTX 4060 Laptop
（GeForce 消费卡）上，dcgm-exporter 能起来、也能看见 GPU，但日志里明确写着

    Not collecting DCP metrics: Profiling is not supported for this group of GPUs or GPU
    Skipping line 21 ('DCGM_FI_PROF_GR_ENGINE_ACTIVE'): metric not enabled
    Skipping line 22 ('DCGM_FI_PROF_PIPE_TENSOR_ACTIVE'): metric not enabled
    Skipping line 23 ('DCGM_FI_PROF_DRAM_ACTIVE'): metric not enabled

而且 `/metrics` 返回 0 行。DCGM 的 profiling 类指标（真正测 SM 活跃度、Tensor Core
活跃度、DRAM 带宽利用率的那批）在 GeForce 上不提供——这是 NVIDIA 的产品分层，不是配置问题。
所以本机只能退到 NVML/nvidia-smi 的设备级指标。

**这个退化带来一个必须说清的口径差异**：`nvidia-smi` 的 `utilization.gpu` 是
「采样周期内**有至少一个 kernel 在执行**的时间占比」，不是 SM 占用率。
于是「GPU 利用率 90% 但吞吐很低」完全可能——kernel 在跑，但每个 kernel 只用到一小部分 SM。
推理服务的 decode 阶段正是这个形态（访存受限）。要区分这两件事，需要 DCGM_FI_PROF_* 或
Nsight，本机拿不到。这也解释了为什么 I3 的归因结论只能落在
「decode 是访存瓶颈 + 批不满时 GPU 在等请求」这个层面，而不能给出 SM 占用率数字。

用法（跑在能看见 GPU 的那一侧，本机是 WSL）：
    python infra/observability/nvidia_smi_exporter.py --port 9401
Prometheus 从容器里抓 host.docker.internal:9401（见同目录 prometheus.yml）。

只用标准库，所以在项目 .venv 和 infra venv 里都能直接跑。
"""
from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# (nvidia-smi 字段, 导出指标名, 单位/类型)
FIELDS: tuple[tuple[str, str, str], ...] = (
    ("utilization.gpu", "nvidia_gpu_utilization_percent", "gauge"),
    ("utilization.memory", "nvidia_gpu_memory_utilization_percent", "gauge"),
    ("memory.used", "nvidia_gpu_memory_used_mib", "gauge"),
    ("memory.total", "nvidia_gpu_memory_total_mib", "gauge"),
    ("temperature.gpu", "nvidia_gpu_temperature_celsius", "gauge"),
    ("power.draw", "nvidia_gpu_power_draw_watts", "gauge"),
    ("clocks.sm", "nvidia_gpu_sm_clock_mhz", "gauge"),
    ("fan.speed", "nvidia_gpu_fan_speed_percent", "gauge"),
)

QUERY = ",".join(f for f, _, _ in FIELDS)

# nvidia-smi 对不支持的字段返回 "N/A"；笔记本卡的 fan.speed 经常就是 N/A。
NUMERIC_RE = re.compile(r"([-+]?\d+(?:\.\d+)?)")


def query_nvidia_smi(timeout_s: float = 10.0) -> list[list[str]]:
    """返回每张卡一行、每行是按 FIELDS 顺序的原始字符串。"""
    binary = shutil.which("nvidia-smi")
    if binary is None:
        raise RuntimeError("找不到 nvidia-smi（是否在能看见 GPU 的一侧运行？）")
    proc = subprocess.run(
        [binary, f"--query-gpu={QUERY}", "--format=csv,noheader,nounits"],
        capture_output=True,
        text=True,
        timeout=timeout_s,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"nvidia-smi 退出码 {proc.returncode}: {proc.stderr.strip()[:200]}")
    return [
        [cell.strip() for cell in line.split(",")]
        for line in proc.stdout.strip().splitlines()
        if line.strip()
    ]


def to_number(raw: str) -> float | None:
    """'45' → 45.0；'N/A' / '[Not Supported]' → None（跳过而不是导出假 0）。"""
    m = NUMERIC_RE.search(raw)
    return float(m.group(1)) if m else None


def render(rows: list[list[str]]) -> str:
    """渲染成 Prometheus 文本格式。取不到值的字段整条跳过。"""
    out: list[str] = []
    for _field, metric, kind in FIELDS:
        out.append(f"# HELP {metric} nvidia-smi {_field}")
        out.append(f"# TYPE {metric} {kind}")
    for gpu_index, row in enumerate(rows):
        for (field, metric, _kind), raw in zip(FIELDS, row):
            value = to_number(raw)
            if value is None:
                continue
            out.append(f'{metric}{{gpu="{gpu_index}",field="{field}"}} {value}')
    return "\n".join(out) + "\n"


class Sampler:
    """带缓存的采样器：抓一次 nvidia-smi 约 200-400ms，别让每个 scrape 都阻塞那么久。"""

    def __init__(self, min_interval_s: float = 2.0) -> None:
        self._min_interval = min_interval_s
        self._lock = threading.Lock()
        self._at = 0.0
        self._body = b""
        self._errors = 0

    def body(self) -> bytes:
        now = time.monotonic()
        with self._lock:
            if self._body and now - self._at < self._min_interval:
                return self._body
            try:
                self._body = render(query_nvidia_smi()).encode("utf-8")
                self._at = now
            except (RuntimeError, subprocess.SubprocessError, OSError):
                self._errors += 1
                if not self._body:
                    raise
            return self._body

    @property
    def errors(self) -> int:
        return self._errors


def make_handler(sampler: Sampler):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - http.server 的接口名
            if self.path.split("?")[0] not in ("/metrics", "/"):
                self.send_response(404)
                self.end_headers()
                return
            try:
                body = sampler.body()
            except RuntimeError as exc:
                self.send_response(503)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.end_headers()
                self.wfile.write(str(exc).encode("utf-8"))
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args) -> None:  # 别把每次 scrape 打到 stderr
            return

    return Handler


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=9401)
    ap.add_argument("--min-interval", type=float, default=2.0)
    args = ap.parse_args(argv)

    sampler = Sampler(args.min_interval)
    sampler.body()  # 启动即失败要立刻报错，别等第一次 scrape 才发现没有 GPU
    server = ThreadingHTTPServer((args.host, args.port), make_handler(sampler))
    print(f"nvidia-smi exporter listening on {args.host}:{args.port}/metrics")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
