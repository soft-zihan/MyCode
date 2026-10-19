"""GPU + vLLM 引擎指标可观测栈（I3）。

一句话：把 vLLM 自己吐的引擎指标（KV cache 用量、排队请求、prefix cache 命中）和
GPU 硬件指标（利用率、显存、温度、功耗）放进同一个 Prometheus，在 Grafana 里同图看，
用来回答「GPU 利用率低到底卡在哪」。

拓扑（本机实测形态）：
    vLLM  ── 跑在 WSL 里（不是容器），监听 0.0.0.0:8123，暴露 /metrics
    Prometheus / Grafana / dcgm-exporter ── 跑在 Docker Desktop 容器里
    容器访问 WSL 宿主：host.docker.internal

起停见同目录 README.md；面板定义在 grafana/dashboards/vllm-gpu.json。
方案背景与结论见 mindmap/mindmaps/28-ai-infra-survey.md。
"""
