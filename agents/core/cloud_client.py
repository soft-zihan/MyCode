"""云端实例地址与 token 的三源解析。服务端转发与 CLI 共用这一份。

配置来源（优先级从高到低）：

- 环境变量 `MYCODE_CLOUD_URL` / `MYCODE_CLOUD_TOKEN`
- `<config.json 同目录>/cloud.json`：`{"url": "...", "token": "..."}`。
  `scripts/cloud-up.sh` 装完集群会写这个文件，所以「一键起云端」之后
  前端的云端面板与 `mycode cloud` 都不需要用户再手填地址和 token。
- 缺省 `http://localhost:8080`（kind 的 extraPortMappings 把 ingress 映到宿主 8080）

放在 agents/core 而不是 routers 下：CLI 不该 import FastAPI 路由模块。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from agents.config import config_path

URL_ENV = "MYCODE_CLOUD_URL"
TOKEN_ENV = "MYCODE_CLOUD_TOKEN"
DEFAULT_URL = "http://localhost:8080"


def _cloud_file() -> Path:
    return config_path().parent / "cloud.json"


def cloud_config() -> tuple[str, str | None, bool]:
    """返回 (url, token, configured)。configured=False 表示 env 与 cloud.json 都没配。"""
    url = os.environ.get(URL_ENV, "").strip()
    token = os.environ.get(TOKEN_ENV, "").strip() or None
    configured = bool(url or token)

    path = _cloud_file()
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        if isinstance(data, dict):
            configured = True
            url = url or str(data.get("url") or "").strip()
            token = token or (str(data.get("token") or "").strip() or None)

    return (url or DEFAULT_URL).rstrip("/"), token, configured