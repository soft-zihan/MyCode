# I5：vLLM 在 K8s 上的部署清单

**状态：清单已写、YAML 可解析；但没有真集群验证过。原因见下，不掩饰。**

## 交付了什么

| 文件 | 内容 |
|---|---|
| `vllm-deployment.yaml` | Deployment：GPU 资源声明、`/dev/shm` 空卷、三段探针（startup/readiness/liveness）、`Recreate` 策略 |
| `vllm-service.yaml` | ClusterIP Service + port-forward 说明 |
| `vllm-hpa.yaml` | HPA v2：以 `vllm_num_requests_waiting`（主）+ `vllm_kv_cache_usage_perc`（兜底）为自定义指标，含扩缩容行为策略。**刻意不用 CPU/利用率类指标**——理由有实测支撑：并发 1→32 时 `utilization.gpu` 均值几乎不动而输出吞吐差 15 倍，见 `infra/observability/README.md`「结论一」 |
| `prometheus-adapter-values.yaml` | 把 vLLM 指标暴露成 K8s custom metrics API 的规则（HPA 的前提）。注意 K8s 指标名**不能含 `:`**，所以 `vllm:num_requests_waiting` 要重写成 `vllm_num_requests_waiting` |

清单里的 vLLM 启动参数来自本机**两处实测**，不是抄来的模板：压测矩阵那组取自
`infra/bench_matrix.py` 的 `build_server_argv`；`--enable-auto-tool-choice` /
`--tool-call-parser` / `--enable-prompt-tokens-details` 取自 MyCode 本体接本地端点跑真实会话
那组（`data/infra/i7/run.sh`）——纯压测负载不带 tools、也不需要读 `cached_tokens`，
所以这三个参数在 `bench_matrix.py` 里没有，但**生产部署必须有**。

## 四个真实踩过的坑，写成了配置而不是注释

1. **`--max-num-seqs` 受 Mamba cache block 数上限约束**：Qwen3.5 是混合架构，每条并发解码序列
   要占一个 cache block；本机 BF16 2B 实测可用块数 230 < 默认 256 → **服务直接拒绝启动**。
   清单里显式写 128 并在注释里说明为什么不能留默认值。
2. **带 tools 的请求必须 `--enable-auto-tool-choice` + 与 chat_template 匹配的 `--tool-call-parser`**：
   缺了会在 API 层被 400 拒掉，而且**引擎侧指标一动不动**（I7 v1 就是这么白跑了一轮，
   表面看像"请求根本没发出去"）。Qwen3.5 的格式是 `qwen3_xml`（模板里用 XML 风格的
   工具调用标签，不是 Hermes 的 JSON 标签）。
3. **模型加载要 ~50s**：就绪/存活探针必须让位给 `startupProbe`（5s × 120 = 600s 上限），
   否则加载期间被反复杀掉、永远起不来。
4. **`--enable-prompt-tokens-details` 默认是关的**：不开则响应体里没有
   `prompt_tokens_details.cached_tokens`，网关侧（以及 MyCode / Langfuse 这类观测面）的
   prefix cache 命中率**一律显示 0%**——缓存照常生效，只是不上报。这是「观测系统静默失明」
   的典型：不报错、指标变 0，很容易被当成「缓存没生效」而去调错地方。
   实测开了之后网关侧与引擎侧三个口径逐位相同（0.8483），见 `data/infra/i7/reconcile.md`。

另外两条容易漏的也写进去了：`/dev/shm` 必须是 `emptyDir.medium: Memory`（vLLM 多进程靠共享内存
通信，默认 64Mi 会崩）；滚动更新用 `Recreate`（推理服务有显存状态，滚动会同时占两份 GPU）。

## 为什么没有真集群验证（诚实边界）

1. **kind 起不来**：`kindest/node:v1.33.1` 镜像拉取两次都卡死在 `Pulling fs layer`
   （累计 40+ 分钟，`timeout` 退出码 124）。同一个 docker.io 上 prometheus/grafana 镜像都正常，
   所以是这一个镜像的连接问题，不是 Docker 坏了。没有集群，`kubectl apply --dry-run=server`
   这类 schema 校验就做不了（`--validate=strict` 需要连 API server 拉 OpenAPI）。
2. **就算有集群，GPU pod 也调度不上**：kind 节点是容器，Docker Desktop 的 WSL2 后端不会把
   NVIDIA 设备插件暴露给嵌套容器；没有 device plugin，`nvidia.com/gpu: 1` 的 pod 永远 Pending。
   而 `vllm/vllm-openai:v0.31.0` 镜像约 10GB，本机也拉不完。
3. **单卡机上 HPA 本来就只能到 1 副本**（每个副本独占一张 GPU），所以"验证扩容"在本地
   物理上无意义；这份清单的价值是**多卡集群上可直接用**，以及把"为什么不能用 CPU 指标扩"
   这个判断写成可执行的配置。

**因此：K8s 这条不写进简历**（与 `08-resume-QA.md` 主张审计表的口径一致：未落地的不写）。
已做的验证只有：8 个 YAML 全部 `yaml.safe_load_all` 通过、`docker compose config` 通过
（observability 栈，且容器真实跑起来过）。

## 如果以后在多卡机器上验证

```bash
kind create cluster --name i5
kubectl apply -f infra/k8s/vllm-deployment.yaml -f infra/k8s/vllm-service.yaml
# 装 NVIDIA device plugin 后 pod 才会从 Pending 起来
kubectl apply -f https://raw.githubusercontent.com/NVIDIA/k8s-device-plugin/main/deployments/static/nvidia-device-plugin.yml
helm install prometheus-adapter prometheus-community/prometheus-adapter \
  -f infra/k8s/prometheus-adapter-values.yaml
kubectl apply -f infra/k8s/vllm-hpa.yaml
kubectl get hpa vllm -w   # 看自定义指标是否真的驱动扩缩容
```
