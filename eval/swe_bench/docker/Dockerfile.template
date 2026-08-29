FROM python:3.11-slim

# 安装基础工具
RUN apt-get update && apt-get install -y \
    git \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# 克隆仓库
ARG REPO_URL
ARG BASE_COMMIT
RUN git clone ${REPO_URL} /workspace/repo
WORKDIR /workspace/repo
RUN git checkout ${BASE_COMMIT}

# 安装项目依赖
RUN if [ -f setup.py ]; then pip install --no-cache-dir -e .; fi
RUN if [ -f pyproject.toml ]; then pip install --no-cache-dir -e .; fi
RUN if [ -f requirements.txt ]; then pip install --no-cache-dir -r requirements.txt; fi

# 安装测试依赖
RUN pip install --no-cache-dir pytest

CMD ["tail", "-f", "/dev/null"]
