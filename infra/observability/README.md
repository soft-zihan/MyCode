# I3：vLLM 引擎指标 + GPU 指标可观测

回答一个面试高频题：**「GPU 利用率上不去，是因为什么？」**——不是背答案，是在自己机器上量出来。
量完的发现是：**在这台机器上这个问法本身就有问题**（见「结论一」）。

## 拓扑（本机实测形态）

```
WSL 里（普通进程，不是容器）              Docker Desktop 容器
├─ vLLM  :8123  /metrics      ──────┐
└─ nvidia_smi_exporter :9401  ───┐  ├──►  Prometheus :9090  ──►  Grafana :3000
                                 └──┘      (host.docker.internal)   面板 vllm-gpu-i3
```

两个「跑在哪」是关键：vLLM 和 exporter 都在 WSL 里（必须能看见 GPU 和项目 venv），
Prometheus/Grafana 在容器里，靠 `host.docker.internal` 反向抓宿主。

## 起停

```bash
# 1) WSL 里起被测服务 + GPU 指标导出器（会挂住，另开终端）
wsl bash data/infra/i3/serve.sh

# 2) Windows 侧起监控栈
cd infra/observability && docker compose up -d

# 3) 造负载（复用 I7b 的采集器当压测客户端）。
#    **一档并发单独跑一段、跑完立刻单独查一段**，别把两档混进同一个统计窗口——
#    本轮就因此得出过一个错结论，见下面「一次自我纠错」。
wsl bash -c "cd /mnt/d/Projects/MyCode && S=\$(date +%s) && \
  for i in \$(seq 1 8); do .venv/bin/python infra/quant_quality.py collect \
    --endpoint http://127.0.0.1:8123/v1 --model q08b \
    --num-prompts 60 --max-tokens 256 --concurrency 16 \
    --out data/infra/i3/load_c16_\$i.jsonl; done && \
  echo QUERY_AT=\$((\$(date +%s)+20))"

# 4) 把归因数字捞出来（别手点面板）。--at/--window 把窗口精确对齐到刚才那一档。
wsl bash -c "cd /mnt/d/Projects/MyCode && .venv/bin/python infra/observability/query_attribution.py \
  --at <上一步打印的 QUERY_AT> --window 130s --out data/infra/i3/attribution_c16.md"

# 5) 收工
cd infra/observability && docker compose down
wsl bash -c "pkill -f 'vllm serve'; pkill -f nvidia_smi_exporter"
```

面板：http://localhost:3000/d/vllm-gpu-i3 （匿名 Viewer 已开，admin/admin）

`query_attribution.py` 跑在哪都行，只要够得着 `:9090`：WSL 侧 `.venv/bin/python` 可以直连
Docker Desktop 发布的端口；Windows 侧 `python` 也可以，但**必须写 `127.0.0.1` 而不是 `localhost`**
（urllib 会先试 `::1` 然后挂住，curl 不会）。落盘一律显式 UTF-8——第一版用 Windows 侧默认编码
重定向，产物是 GBK 乱码，文档和产物对不上就是这么来的。

## 实测归因（Qwen3.5-0.8B，同一份负载、同一个服务、prefix caching 全开，只改客户端并发）

三档各跑一个**独立统计窗口**（c1 = 90 请求 / 236s，c16 = 480 请求 / 120s，c32 = 600 请求 / 103s），
产物分别是 `data/infra/i3/attribution_c1.md`、`attribution_c16.md`、`attribution_c32.md`：

| 观测 | 并发 1 | 并发 16 | 并发 32 | 怎么读 |
|---|---|---|---|---|
| **GPU 利用率 均值 %** | **57.2** | **60.2** | **54.9** | ← 几乎不动 |
| GPU 利用率 峰值 % | 71 | 79 | 90 | 峰值随并发涨，均值不涨 |
| **输出吞吐 峰值 tok/s** | **99** | **1021** | **1492** | ← 同一时间涨了 **15 倍** |
| 输入吞吐 峰值 tok/s | 396 | 3244 | 4649 | |
| **每迭代 token 数 均值** | **1.9** | **26.9** | **53.6** | 真正随并发线性走的量 |
| 每迭代 token 数 p50 | 0.50 | 12.0 | 24.1 | ≈ 并发数；见下方分位数的坑 |
| 引擎迭代次数（窗口内） | 24350 | 9158 | 5601 | 迭代变少、每次干的活变多 = 摊薄固定开销 |
| vLLM KV cache 使用率 峰值 % | 1.6 | 10.7 | 23.2 | 32 路也只用掉 1/4 |
| 并发 running 峰值 | 1 | 16 | 32 | 引擎照单全收 |
| 排队 waiting 峰值 / 抢占 | 0 / 0 | 1 / 0 | 0 / 0 | **一次都没排队、没抢占** |
| 平均 decode 耗时 s | 2.55 | 3.39 | 4.53 | 单请求变慢（跷跷板） |
| TTFT p50 s | 0.054 | 0.208 | 0.362 | 首字延迟 6.7 倍差 |
| 平均排队耗时 s | 2e-5 | 0.012 | 0.069 | 开始排队，但量级仍可忽略 |
| 显存已用 峰值 MiB | 7227 | 7227 | 7227 | 三档**完全相同**：显存由预算参数决定，不由负载决定 |
| 显存带宽利用率 峰值 % | 79 | 88 | 87 | 高于 SM 利用率峰值 → 访存受限 |
| prefix 命中率（token 级） | 0.703 | 0.727 | 0.727 | 缓存已预热，与 I7 对账口径一致 |
| 功耗 峰值 W | 65.7 | 80.3 | 67.9 | **不单调 → 别拿单点功耗下结论** |
| SM 频率 峰值 MHz | 2475 | 2385 | 2475 | 满速，没有降频 |

### 结论一：「GPU 利用率上不去是因为什么」——这台机器上这个问题问错了

利用率均值在三档之间是 57 / 60 / 55%，**看起来毫无变化**；而同一时间真实产出（输出吞吐）
涨了 15 倍。原因是 `nvidia-smi` 的 `utilization.gpu` 口径是「采样周期内**有至少一个 kernel
在执行**的时间占比」——并发 1 时 decode 一步接一步地发 kernel，这个值照样很高，
但每个 kernel 只用到一小片 SM。**它不是 SM 占用率，不能拿来判断 GPU 吃没吃饱。**

判断吃饱没有，要看引擎侧：每迭代 token 数（1.9 → 26.9 → 53.6）、KV 占用（1.6% → 23.2%）、
排队与抢占（≈0）。

### 结论二：瓶颈是「客户端给的活不够」，不是显存、不是算力、不是降频

- **不是在等显存**：并发 32 时 KV cache 峰值只用 23.2%，零排队零抢占。这正好解释了 I2 里
  「`gpu-memory-utilization` 从 0.85 调到 0.70，吞吐变化落在噪声里」——显存本来就不是瓶颈。
- **不是算不动 / 不是降频**：SM 频率 2385–2475 MHz 满速，功耗最高 80W 已接近这张笔记本卡的 TGP。
- **是批没填满**：`max_num_seqs=256`，只压到 32 路；每迭代 token 数 p50 才 24。
  所以优化方向是**把批做大**（更高并发、更长输出、把多个 agent 的请求合流）
  或**减少 decode 步数**（投机解码 / 更短输出），**而不是**加显存或调 `gpu-memory-utilization`。
- 代价也量化了：并发 1 → 32，TTFT p50 从 54ms 涨到 362ms、单请求 decode 从 2.55s 涨到 4.53s。
  这就是 1.6 节那个跷跷板，**吞吐和延迟必须一起报**。

时间花在哪也清楚：decode 2.5–4.5s vs prefill 0.06–0.21s（差 20 倍以上），而 decode 是访存瓶颈
（显存带宽利用率峰值 88% > SM 利用率峰值 79%）——这与 I2 里「FP8 的收益全在 decode 侧
（TPOT -24%）、prefill 侧几乎不动」是同一件事的两个观测面。

### 一次自我纠错（比上面任何一个数字都值钱）

第一版的结论是「GPU 利用率均值只有 13%、平均每迭代只处理 7.1 个 token → GPU 在等请求」。
两个数都错，错法是同一个：**统计窗口里混进了两档负载**。那一轮先跑并发 1（30 请求，串行 ~95s）
再跑并发 16（60 请求，~15s），然后用一个 15 分钟窗口一次性查询——于是：

- 均值 13% 里大头是**并发 1 阶段和阶段之间的空闲**，不是「GPU 没活干」；
- 每迭代 7.1 token 是「~7700 次单 token 迭代」和「~1500 次 16 token 迭代」的**混合平均**，
  两档谁都不代表。

复测把每档并发单独跑、单独查（`--at` + `--window` 精确对齐），得到的才是上面那张表：
并发 16 的每迭代 token 均值其实是 **26.9**，不是 7.1；并发 1 的利用率均值其实是 **57%**，不是 13%。

顺带暴露的第二个问题：**头号结论曾经没有产物支撑**。`query.py` 里根本没有「每迭代 token 数」
这条查询，7.1 是从面板上肉眼读的，落盘的 `attribution.md` 里没有这一行。现在这条查询进了
`query_attribution.py`（有单测钉住），每个数字都能从命令行重跑出来。

> 教训：**指标的时间窗口必须和负载的时间窗口对齐**，尤其是「先低并发再高并发」这种
> 阶梯式压测；以及**写进文档的每个数字都要有能重跑的查询和落盘的产物**，面板截图不算。

### 分位数的坑

`iteration_tokens_total` 的桶边界是 1 / 8 / 16 / 32 / 64 / …，很粗。`histogram_quantile`
在最低桶内按 [0, 1] 线性插值，所以并发 1 那档的 p50 显示 **0.50**——不是「半个 token」，
而是「半数以上的迭代落在 ≤1 token 的桶里」。**下结论用均值（sum/count），分位数只用来看形状。**

## 两个诚实边界

**① DCGM 在 GeForce 上不可用，所以拿不到真正的 SM 占用率。**
`nvcr.io/nvidia/k8s/dcgm-exporter:latest` 能起、也能看见 GPU（`nvidia-smi -L` 在容器里正常），但日志明确写：

```
Not collecting DCP metrics: Profiling is not supported for this group of GPUs or GPU
Skipping line 21 ('DCGM_FI_PROF_GR_ENGINE_ACTIVE'): metric not enabled
Skipping line 22 ('DCGM_FI_PROF_PIPE_TENSOR_ACTIVE'): metric not enabled
Skipping line 23 ('DCGM_FI_PROF_DRAM_ACTIVE'): metric not enabled
```

且 `/metrics` 返回 **0 行**。DCGM 的 profiling 类指标在消费卡上不提供，是产品分层不是配置问题。
所以 GPU 侧退化为 `nvidia_smi_exporter.py`。
（另外 nvcr.io 上 `4.2.0-4.4.0-ubuntu22.04` / `4.1.0-4.3.0-ubuntu22.04` / `3.3.9-3.6.0-ubuntu22.04`
这些 tag 都**不存在**，只有 `latest` 能拉。）

**② 因此 `utilization.gpu` 的口径要说清**：nvidia-smi 的这个值是
「采样周期内**有至少一个 kernel 在执行**的时间占比」，**不是 SM 占用率**。
「利用率 90% 但吞吐很低」完全可能——kernel 在跑，但每个 kernel 只用到一小部分 SM。
这次它正好当了反面教材：三档并发下均值 57 / 60 / 55% 几乎不动，输出吞吐却差 15 倍。
真正撑起归因的是引擎侧硬数据——每迭代 token 数 1.9 → 26.9 → 53.6、KV 峰值 1.6% → 23.2%、
零排队零抢占、decode/prefill 20 倍以上。
**代价要说清**：没有 DCGM 就拿不到 SM 占用率这个「正确答案」，
所以本轮只能证明「常看的那个指标不可信」，不能给出真正的占用率。

**③ 有两个计数器在本配置下恒为 0**：`vllm:estimated_flops_per_gpu_total` 与
`vllm:estimated_read_bytes_per_gpu_total`。本来想用它们直接算算术强度（FLOP/byte）来证明
「decode 是访存瓶颈」，实测不填充（疑似需要 `--enable-per-request-metrics`，未验证），
所以面板换成了「批饱满度」。要讲访存受限，目前只有 `utilization.memory` 这个粗指标 + 原理推断。

**④ 峰值类指标噪声大，不能单独下结论**：功耗峰值三档是 65.7 / 80.3 / 67.9 W，**不单调**
（15s 采样一次，峰值就是撞运气）。所以「没有降频」这条结论用的是 SM 频率（2385–2475 MHz，
三档都满速），不是功耗。同理，显存已用峰值三档都是 7227 MiB——那个是**预算决定的常数**，
不是负载信号，别把它当「显存占用随并发变化」来读。

**⑤ 三档窗口都不是「纯稳态」**：负载是用采集器分 8–10 批顺序发的，批与批之间有 1–2s 的
客户端重启空档，所以并发 16 那档的 `running` 峰值虽是 16，每迭代 token p50 只有 12
（不是 16）。要看纯稳态得换一个持续发压的客户端。

## 文件

| 文件 | 作用 |
|---|---|
| `docker-compose.yml` | Prometheus + Grafana（**没有 dcgm-exporter**，原因见上） |
| `prometheus.yml` | 两个 scrape target，都走 `host.docker.internal` |
| `nvidia_smi_exporter.py` | GPU 指标导出器，纯标准库，9 项单测 |
| `query_attribution.py` | 把归因数字从 Prometheus 捞成 markdown 产物，10 项单测；`--at/--window` 用来把窗口对齐到单独一档负载 |
| `grafana/provisioning/` | 数据源（uid 固定为 `prometheus`）+ 面板自动加载 |
| `grafana/dashboards/vllm-gpu.json` | 8 个面板，27 条查询全部实测有数据 |

单测：`.venv/bin/python -m pytest tests/unit/test_nvidia_smi_exporter.py tests/unit/test_observability_query_attribution.py -q`

