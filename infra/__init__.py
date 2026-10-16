"""AI Infra 本地实证实验区（推理引擎部署与压测，方案见 mindmap/mindmaps/28-ai-infra-survey.md）。

依赖隔离：vLLM/torch 装在 WSL 侧独立 venv（``~/venvs/infra``），
不进主 requirements.txt，也不进项目 .venv——产品运行环境不受实验影响。
实验产物统一落 ``data/infra/``（data/ 已 gitignore）。
"""
